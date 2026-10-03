"""다운로드 메타데이터(JSON) → YAML 변환.

gallery-dl 은 ``--write-info-json`` 으로 JSON(info.json)만 생성하므로,
앱이 그 JSON 을 읽어 사람이 읽기 좋은 YAML 을 ``.metadata`` 하위에 추가 생성한다.
(JSON 은 항상 유지되는 원본이며, YAML 은 선택적 파생물.)
"""

import json
import os

from . import utils

try:  # pragma: no cover - 선택적 의존성
    import yaml as _yaml
except Exception:  # pragma: no cover
    _yaml = None


def is_available() -> bool:
    return _yaml is not None


def to_yaml(data) -> str:
    if _yaml is None:
        return ""
    return _yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def info_json_to_yaml(info_path: str, out_path: str | None = None) -> str | None:
    """info.json 파일을 읽어 YAML 로 변환(기본: 같은 이름의 .yaml)."""
    if _yaml is None or not info_path or not os.path.isfile(info_path):
        return None
    try:
        with open(info_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        try:
            with open(info_path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except Exception:
            return None
    if out_path is None:
        base = info_path
        lowered = base.lower()
        if lowered.endswith(".info.json"):
            base = base[: -len(".info.json")]
        elif lowered.endswith(".json"):
            base = base[: -len(".json")]
        out_path = base + ".yaml"
    utils.ensure_dir(os.path.dirname(out_path) or ".")
    try:
        with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(to_yaml(data))
    except Exception:
        return None
    return out_path


def _is_meta_json(name: str) -> bool:
    lowered = name.lower()
    if lowered == "info.json":
        return True
    return lowered.endswith(".json") and not lowered.endswith(".json.tmp")


def relocate_metadata(directory: str) -> int:
    """미디어 옆에 생성된 메타 JSON 을 ``.metadata/<상대경로>`` 로 이동.

    gallery-dl 의 --write-info-json / metadata 포스트프로세서는 파일 옆에 JSON 을
    쓰므로, 앱이 수집 후 격리 폴더로 옮긴다(기존 메타데이터 격리 정책 유지).
    반환: 이동한 파일 수.
    """
    if not directory or not os.path.isdir(directory):
        return 0
    meta_root = os.path.join(directory, utils.METADATA_DIRNAME)
    moved = 0
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d != utils.METADATA_DIRNAME]
        for name in files:
            if not _is_meta_json(name):
                continue
            src = os.path.join(root, name)
            rel = os.path.relpath(src, directory)
            dst = os.path.join(meta_root, rel)
            try:
                utils.ensure_dir(os.path.dirname(dst) or ".")
                if os.path.exists(dst):
                    os.remove(dst)
                os.replace(src, dst)
                moved += 1
            except OSError:
                continue
    return moved


def _iter_info_files(directory: str):
    if not directory or not os.path.isdir(directory):
        return
    for root, _dirs, files in os.walk(directory):
        for name in files:
            if name.lower().endswith(".info.json") or (
                name.lower().endswith(".json") and not name.lower().endswith(".json.tmp")
            ):
                yield os.path.join(root, name)


def convert_directory(directory: str) -> int:
    """디렉터리 내 모든 info.json 을 YAML 로 변환. 변환 개수 반환."""
    if _yaml is None:
        return 0
    count = 0
    for info_path in _iter_info_files(directory):
        if info_json_to_yaml(info_path) is not None:
            count += 1
    return count


def _iter_stray(directory: str):
    """`.metadata` 밖에 남아 있는 메타 JSON 경로를 순회."""
    if not directory or not os.path.isdir(directory):
        return
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d != utils.METADATA_DIRNAME]
        for name in files:
            if _is_meta_json(name):
                yield os.path.join(root, name)


def scan_directory(directory: str) -> dict:
    """이동 없이 미격리 메타 JSON 개수만 센다 (dry-run)."""
    count = sum(1 for _ in _iter_stray(directory))
    return {"directory": directory, "json": count}


def migrate_directory(directory: str, yaml: bool = False) -> dict:
    """레거시 메타데이터를 `.metadata` 로 격리(+선택 YAML 변환). 멱등."""
    stray_before = sum(1 for _ in _iter_stray(directory))
    moved = relocate_metadata(directory)
    converted = 0
    if yaml and _yaml is not None:
        converted = convert_directory(directory)
    return {
        "directory": directory,
        "stray_before": stray_before,
        "moved": moved,
        "converted": converted,
    }
