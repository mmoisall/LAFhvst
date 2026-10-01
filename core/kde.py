"""KDE(커널 밀도 추정) 기반 업로드 확률/스케줄 학습 (표준 라이브러리만 사용).

- 업로드 시각 이력(요일/시각)을 주간(168h) 원형 가우시안 KDE로 추정한다.
- '앞으로 다가올 base 주기 구간'의 평균 밀도를 구해, 밀도가 높으면(자주 올리는
  시간대) 다음 대기 시간을 짧게, 낮으면(휴면 시간대) 길게 만든다.
"""

import json
import math

WEEK_HOURS = 24.0 * 7.0
DEFAULT_BANDWIDTH_HOURS = 1.5
DEFAULT_BASE_CYCLE_MINUTES = 60
MIN_BASE_CYCLE_MINUTES = 10
MAX_BASE_CYCLE_MINUTES = 1440
MIN_WAIT_MINUTES = 5
MAX_WAIT_MINUTES = 1440
WEIGHT_MIN = 0.5
WEIGHT_MAX = 2.0
CYCLE_SHRINK = 0.9
CYCLE_GROW = 1.1
HISTORY_LIMIT = 50000
UPCOMING_SAMPLES = 8
GRID_DAY_NAMES = ["월", "화", "수", "목", "금", "토", "일"]


def clamp(value, low, high):
    return max(low, min(high, value))


def parse_history(raw) -> list[dict]:
    if not raw:
        return []
    data = raw
    if not isinstance(data, list):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(data, list):
        return []
    result: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            day = int(entry.get("day")) % 7
            hour = float(entry.get("hour")) % 24.0
        except (TypeError, ValueError):
            continue
        result.append({"day": day, "hour": hour})
    return result


def dump_history(entries) -> str:
    return json.dumps(list(entries or []), ensure_ascii=False)


def weektime(day, hour) -> float:
    return (float(day) % 7.0) * 24.0 + (float(hour) % 24.0)


def history_points(entries) -> list[float]:
    return [weektime(entry.get("day", 0), entry.get("hour", 0.0)) for entry in entries or []]


def _circular_delta(a: float, b: float) -> float:
    delta = abs(a - b) % WEEK_HOURS
    return min(delta, WEEK_HOURS - delta)


def density_at(points, x, bandwidth: float = DEFAULT_BANDWIDTH_HOURS) -> float:
    if not points:
        return 0.0
    h = max(0.05, float(bandwidth))
    norm = 1.0 / (len(points) * h * math.sqrt(2.0 * math.pi))
    total = 0.0
    for point in points:
        delta = _circular_delta(x, point)
        total += math.exp(-(delta * delta) / (2.0 * h * h))
    return norm * total


def max_density(points, bandwidth: float = DEFAULT_BANDWIDTH_HOURS, step: float = 1.0) -> float:
    if not points:
        return 0.0
    best = 0.0
    x = 0.0
    while x < WEEK_HOURS:
        value = density_at(points, x, bandwidth)
        if value > best:
            best = value
        x += step
    return best


def upcoming_mean_density(
    points,
    start_weektime: float,
    window_hours: float,
    bandwidth: float = DEFAULT_BANDWIDTH_HOURS,
    samples: int = UPCOMING_SAMPLES,
) -> float:
    if not points:
        return 0.0
    samples = max(1, int(samples))
    window = max(0.05, float(window_hours))
    total = 0.0
    for index in range(samples):
        fraction = (index + 0.5) / samples
        x = (start_weektime + window * fraction) % WEEK_HOURS
        total += density_at(points, x, bandwidth)
    return total / samples


def weight_from_density(relative: float) -> float:
    r = clamp(float(relative), 0.0, 1.0)
    return WEIGHT_MAX - (WEIGHT_MAX - WEIGHT_MIN) * r


def next_wait_minutes(base_cycle, relative_density) -> float:
    base = clamp(
        float(base_cycle or DEFAULT_BASE_CYCLE_MINUTES),
        MIN_BASE_CYCLE_MINUTES,
        MAX_BASE_CYCLE_MINUTES,
    )
    return clamp(base * weight_from_density(relative_density), MIN_WAIT_MINUTES, MAX_WAIT_MINUTES)


def update_cycle_ema(current, found: bool) -> int:
    base = float(current) if current and current > 0 else DEFAULT_BASE_CYCLE_MINUTES
    base = base * (CYCLE_SHRINK if found else CYCLE_GROW)
    return int(round(clamp(base, MIN_BASE_CYCLE_MINUTES, MAX_BASE_CYCLE_MINUTES)))


def state_label(relative_density: float, next_wait) -> str:
    minutes = max(1, int(round(next_wait)))
    if relative_density >= 0.6:
        prefix = "집중 관찰 중"
    elif relative_density <= 0.2:
        prefix = "휴면기"
    else:
        prefix = "관찰 중"
    if minutes < 60:
        return "%s(%d분)" % (prefix, minutes)
    return "%s(%.1f시간)" % (prefix, minutes / 60.0)


def predict(history_entries, base_cycle, now, bandwidth: float = DEFAULT_BANDWIDTH_HOURS) -> dict:
    points = history_points(history_entries)
    base = clamp(
        float(base_cycle or DEFAULT_BASE_CYCLE_MINUTES),
        MIN_BASE_CYCLE_MINUTES,
        MAX_BASE_CYCLE_MINUTES,
    )
    start = weektime(now.weekday(), now.hour + now.minute / 60.0 + now.second / 3600.0)
    window_hours = base / 60.0
    if points:
        peak = max_density(points, bandwidth, step=1.0)
        mean = upcoming_mean_density(points, start, window_hours, bandwidth)
        relative = clamp((mean / peak) if peak > 0 else 0.0, 0.0, 1.0)
    else:
        peak = 0.0
        mean = 0.0
        relative = 0.5
    wait = next_wait_minutes(base, relative)
    return {
        "base_cycle": int(round(base)),
        "density": mean,
        "peak_density": peak,
        "relative_density": round(relative, 4),
        "weight": round(weight_from_density(relative), 3),
        "next_wait": int(round(wait)),
        "label": state_label(relative, wait),
        "history_count": len(points),
    }


def density_grid(history_entries, bandwidth: float = DEFAULT_BANDWIDTH_HOURS) -> list[list[float]]:
    points = history_points(history_entries)
    peak = max_density(points, bandwidth, step=1.0)
    grid: list[list[float]] = []
    for day in range(7):
        row: list[float] = []
        for hour in range(24):
            value = density_at(points, weektime(day, hour + 0.5), bandwidth) if points else 0.0
            normalized = (value / peak) if peak > 0 else 0.0
            row.append(round(clamp(normalized, 0.0, 1.0), 4))
        grid.append(row)
    return grid
