"""추천 페이지용 게시물 인덱스 (구축/조회).

수집된 미디어와 gallery-dl 메타데이터(``.metadata/*.json``)를 훑어 게시물 단위
행(``post_index``)을 만들고, 최근글·인기글·드문 업로더 섹션을 계산한다.

- 메타데이터가 있으면 반응 지표(좋아요/리트윗/북마크/조회 등)까지 적재한다.
- 없으면 파일명 ``{date} - {author} - {site} - {post_id}-{n}(...).ext`` 에서
  날짜·작성자·게시물ID를 뽑고 날짜가 없으면 파일 mtime 을 쓴다.
"""

import json
import os
import re
import statistics
from datetime import datetime, timedelta

from . import utils
from .models import PostIndex, select

MEDIA_EXTENSIONS = set(utils.IMAGE_EXTENSIONS) | {
    ".mp4",
    ".webm",
    ".mkv",
    ".mov",
    ".m4v",
    ".avi",
}

DEFAULT_WEIGHTS = {
    "like": 1.0,
    "retweet": 2.0,
    "bookmark": 2.0,
    "reply": 1.0,
    "quote": 1.0,
    "view": 0.001,
}

_ID_KEYS = ("tweet_id", "id", "post_id", "illust_id", "media_id", "key", "shortcode")
_TITLE_KEYS = ("content", "description", "title", "caption", "comment", "text")
_TAG_KEYS = ("tags", "tag_list", "tag", "tag_string")
_URL_KEYS = ("post_url", "webpage_url", "url")

_METRIC_KEYS = {
    "likes": ("favorite_count", "like_count", "likes", "favorites", "score"),
    "retweets": ("retweet_count", "reblogs", "reblog_count", "shares"),
    "replies": ("reply_count", "comment_count", "comments", "replies"),
    "quotes": ("quote_count",),
    "bookmarks": ("bookmark_count", "bookmarked_count", "bookmarks"),
    "views": ("view_count", "views", "play_count"),
}

_DATE_KEYS = (
    "date",
    "upload_date",
    "created_at",
    "published",
    "published_at",
    "taken_at",
    "post_date",
    "timestamp",
)

_FILENAME_HEAD = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}[ T]\d{2}:?\d{2}:?\d{2}) - (?P<rest>.+)$"
)
_FILENAME_TAIL = re.compile(r"(?P<post>[^-()/\\]+)-(?P<num>\d+)\((?P<inner>[^)]*)\)$")

_URL_TEMPLATES = {
    "twitter": "https://x.com/{author}/status/{post}",
    "pixiv": "https://www.pixiv.net/artworks/{post}",
    "bluesky": "https://bsky.app/profile/{author}",
}


# --------------------------------------------------------------------- helpers
def _pick(info, keys):
    for key in keys:
        if key in info and info[key] is not None:
            return info[key]
    return None


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _parse_dt(raw):
    """gallery-dl info 의 날짜 필드를 naive datetime 으로."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value > 1e11:
            value = value / 1000.0
        try:
            return datetime.fromtimestamp(value)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        return _parse_dt(int(text))
    normalized = text.replace("Z", "").replace("T", " ").split("+")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return utils.parse_datetime(text)


def _info_datetime(info):
    for key in _DATE_KEYS:
        parsed = _parse_dt(info.get(key))
        if parsed is not None:
            return parsed
    return None


def _tags_of(info):
    raw = _pick(info, _TAG_KEYS)
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = list(raw.keys())
    if isinstance(raw, str):
        raw = [part for part in re.split(r"[,\s]+", raw) if part]
    if isinstance(raw, (list, tuple)):
        return [_as_text(item) for item in raw if _as_text(item)]
    return []


def _uploader_of(info, source):
    name = ""
    key = ""
    for field in ("user", "author", "uploader", "username", "artist", "creator"):
        value = info.get(field)
        if isinstance(value, dict):
            name = _as_text(value.get("name") or value.get("nick") or value.get("username"))
            key = _as_text(value.get("id") or value.get("key"))
        elif value:
            name = _as_text(value)
        if name or key:
            break
    return key or _as_text(source.get("key")), name or _as_text(source.get("name"))


def parse_filename(name):
    """``{date} - {author} - {site} - {post_id}-{n}(...).ext`` 에서 정보를 뽑는다."""
    stem = os.path.splitext(name)[0]
    head = _FILENAME_HEAD.match(stem)
    if not head:
        return {}
    posted = None
    raw_date = head.group("date").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H%M%S"):
        try:
            posted = datetime.strptime(raw_date, fmt)
            break
        except ValueError:
            continue
    rest = head.group("rest")
    tail = _FILENAME_TAIL.search(rest)
    if not tail:
        return {"posted_at": posted, "author": rest.strip()}
    prefix = rest[: tail.start()].rstrip(" -")
    author, _, site = prefix.rpartition(" - ")
    post = tail.group("post").strip()
    # 구형 HVST 포맷은 `{제목}-{n}({post_id}_p{page})` 라 제목이 post 자리에 온다.
    # 괄호 안이 `숫자`/`숫자_p숫자` 이고 post 에 숫자가 없으면 그 숫자가 실제 게시물 ID 다.
    inner = tail.group("inner").strip()
    legacy_id = re.fullmatch(r"(\d+)(?:_p\d+)?", inner)
    if legacy_id and not re.search(r"\d", post):
        post = legacy_id.group(1)
    return {
        "posted_at": posted,
        "author": author.strip(),
        "site": site.strip(),
        "post_id": post,
    }


def _load_info(path):
    if not path or not os.path.isfile(path):
        return None
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else None
        except Exception:
            continue
    return None


def _post_url(site, author, post_id):
    template = _URL_TEMPLATES.get((site or "").lower())
    if not template:
        return None
    try:
        return template.format(author=author or "_", post=post_id or "")
    except Exception:
        return None


def _engagement(counts, weights):
    total = 0.0
    for field, weight in weights.items():
        total += float(counts.get(field, 0) or 0) * float(weight or 0)
    return total


def _row_engagement(row, weights):
    return _engagement(
        {field: getattr(row, field, 0) or 0 for field in _METRIC_KEYS}, weights
    )


def _post_from_media(path, rel, name, info, source):
    site = (source.get("site") or "").strip().lower()
    uploader_key = _as_text(source.get("key"))
    uploader_name = _as_text(source.get("name"))
    post_id = ""
    posted = None
    title = None
    tags = []
    post_url = None
    sensitive = False
    counts = {field: 0 for field in _METRIC_KEYS}
    has_engagement = False
    origin = "filename"

    if isinstance(info, dict):
        origin = "metadata"
        post_id = _as_text(_pick(info, _ID_KEYS))
        posted = _info_datetime(info)
        title = _as_text(_pick(info, _TITLE_KEYS)) or None
        tags = _tags_of(info)
        sensitive = bool(info.get("sensitive"))
        info_key, info_name = _uploader_of(info, source)
        uploader_key = info_key or uploader_key
        uploader_name = info_name or uploader_name
        site = _as_text(info.get("category") or info.get("site") or site).lower()
        post_url = _as_text(_pick(info, _URL_KEYS)) or None
        for field, keys in _METRIC_KEYS.items():
            value = _as_int(_pick(info, keys))
            if value is not None:
                counts[field] = value
                has_engagement = True

    if not post_id or not posted:
        parsed = parse_filename(name)
        if not post_id:
            post_id = _as_text(parsed.get("post_id"))
        if posted is None:
            posted = parsed.get("posted_at")
        if not uploader_name:
            uploader_name = _as_text(parsed.get("author")) or uploader_name
        if not site:
            site = _as_text(parsed.get("site")).lower() or site
        if parsed.get("author") and not uploader_key:
            uploader_key = _as_text(parsed["author"])

    if posted is None:
        try:
            posted = datetime.fromtimestamp(os.path.getmtime(path))
        except OSError:
            posted = None

    if not post_id:
        post_id = os.path.splitext(name)[0]

    if not post_url:
        post_url = _post_url(site, uploader_name or uploader_key, post_id)

    return {
        "site": site or "unknown",
        "uploader_key": uploader_key or uploader_name or "",
        "uploader_name": uploader_name or uploader_key or "",
        "post_id": post_id,
        "post_url": post_url,
        "posted_at": posted,
        "title": title,
        "media_count": 1,
        "tags": tags,
        "counts": counts,
        "has_engagement": has_engagement,
        "sensitive": sensitive,
        "media_rel": rel,
        "origin": origin,
    }


def iter_source_posts(directory, source):
    """소스 디렉터리를 한 번 훑어 게시물 dict 목록을 만든다(게시물 단위 병합)."""
    if not directory or not os.path.isdir(directory):
        return []
    posts = {}
    for root, dirs, files in os.walk(directory):
        dirs[:] = [name for name in dirs if name != utils.METADATA_DIRNAME]
        for name in files:
            lowered = name.lower()
            if lowered.endswith(".part") or lowered.endswith(".json.tmp"):
                continue
            if os.path.splitext(lowered)[1] not in MEDIA_EXTENSIONS:
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, directory)
            info = _load_info(utils._find_info_path(directory, path))
            entry = _post_from_media(path, rel, name, info, source)
            key = entry["post_id"]
            existing = posts.get(key)
            if existing is not None:
                existing["media_count"] += 1
                if not existing.get("media_rel"):
                    existing["media_rel"] = rel
                if existing["origin"] != "metadata" and entry["origin"] == "metadata":
                    posts[key] = entry
                    entry["media_count"] = existing["media_count"]
                continue
            posts[key] = entry
    return list(posts.values())


def _apply(row, post):
    row.site = post["site"]
    row.uploader_key = post["uploader_key"]
    row.uploader_name = post["uploader_name"]
    row.post_url = post["post_url"]
    row.posted_at = post["posted_at"]
    row.title = post["title"]
    row.media_count = post["media_count"]
    row.tags = json.dumps(post["tags"], ensure_ascii=False)
    for field, value in post["counts"].items():
        setattr(row, field, value)
    row.has_engagement = post["has_engagement"]
    row.sensitive = post["sensitive"]
    row.media_rel = post["media_rel"]
    row.origin = post["origin"]


# ------------------------------------------------------------------- indexing
def refresh_source(session, source, prune=True):
    """소스 하나를 (재)색인한다. source 는 ``models.source_to_dict`` 결과."""
    source_id = source.get("id")
    directory = source.get("download_directory")
    posts = iter_source_posts(directory, source)

    def _key(post):
        site = post["site"]
        post_id = post["post_id"]
        if site == "unknown":
            post_id = "%s:%s" % (source_id, post_id)
        return site, post_id

    keys = {_key(post) for post in posts}
    # (site, post_id) 는 전역 UNIQUE 제약이라, 이 소스에 없는(=다른 소스가 가진) 행도 함께 찾아야
    # INSERT 충돌이 나지 않는다. 자기 소스 행만 갱신/정리 대상으로 삼는다.
    existing: dict = {}
    if keys:
        rows = (
            session.execute(
                select(PostIndex)
                .where(PostIndex.site.in_({key[0] for key in keys}))
                .where(PostIndex.post_id.in_({key[1] for key in keys}))
            )
            .scalars()
            .all()
        )
        for row in rows:
            key = (row.site, row.post_id)
            if key in keys and key not in existing:
                existing[key] = row
    owned = {key: row for key, row in existing.items() if row.source_id == source_id}

    created = updated = skipped = 0
    seen = set()
    for post in posts:
        site = post["site"]
        post_id = post["post_id"]
        if site == "unknown":
            post_id = "%s:%s" % (source_id, post_id)
        key = (site, post_id)
        seen.add(key)
        row = existing.get(key)
        if row is None:
            row = PostIndex(source_id=source_id, site=site, post_id=post_id)
            session.add(row)
            existing[key] = row
            created += 1
        elif row.source_id != source_id:
            # 같은 게시물을 이미 다른 소스가 색인했다 → 출처/media_rel 이 어긋나지 않게 그대로 둔다
            skipped += 1
            continue
        else:
            updated += 1
        _apply(row, post)
    removed = 0
    if prune:
        for key, row in owned.items():
            if key not in seen:
                session.delete(row)
                removed += 1
    session.commit()
    return {
        "source_id": source_id,
        "name": source.get("name"),
        "directory": directory,
        "found": len(posts),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "removed": removed,
    }


def refresh_all(session, sources=None, prune=True):
    """모든 소스를 (재)색인한다."""
    from . import models

    if sources is None:
        sources = models.list_all_sources(session)
    results = [refresh_source(session, source, prune=prune) for source in sources]
    return {
        "sources": len(results),
        "created": sum(item["created"] for item in results),
        "updated": sum(item["updated"] for item in results),
        "removed": sum(item["removed"] for item in results),
        "details": results,
    }


# -------------------------------------------------------------------- queries
def _window_rows(session, days, site=None, include_sensitive=False, query=None):
    statement = select(PostIndex)
    if days and days > 0:
        start = datetime.now() - timedelta(days=int(days))
        statement = statement.where(PostIndex.posted_at.is_not(None)).where(
            PostIndex.posted_at >= start
        )
    if site:
        statement = statement.where(PostIndex.site == site)
    if not include_sensitive:
        statement = statement.where(PostIndex.sensitive.is_(False))
    rows = list(session.execute(statement).scalars().all())
    if query:
        needle = query.strip().lower()
        rows = [
            row
            for row in rows
            if needle in (row.uploader_name or "").lower()
            or needle in (row.title or "").lower()
            or needle in (row.post_id or "").lower()
            or needle in (row.site or "").lower()
        ]
    return rows


def weights_from_settings(settings):
    """설정에서 반응 지표 가중치를 읽는다(없으면 기본값)."""
    weights = dict(DEFAULT_WEIGHTS)
    mapping = {
        "like": "recommendWeightLike",
        "retweet": "recommendWeightRetweet",
        "bookmark": "recommendWeightBookmark",
        "reply": "recommendWeightReply",
        "quote": "recommendWeightQuote",
        "view": "recommendWeightView",
    }
    for field, key in mapping.items():
        value = (settings or {}).get(key)
        if value is None:
            continue
        try:
            weights[field] = float(value)
        except (TypeError, ValueError):
            continue
    return weights


def _normalized_scores(rows, weights):
    """(site, 기간) 내 engagement 백분위 점수(0..1)."""
    grouped = {}
    for row in rows:
        grouped.setdefault(row.site or "", []).append(row)
    scores = {}
    for items in grouped.values():
        values = sorted({_row_engagement(item, weights) for item in items})
        rank = {value: index for index, value in enumerate(values)}
        span = len(values) - 1
        for item in items:
            value = _row_engagement(item, weights)
            scores[item.id] = (rank[value] / span) if span else 1.0
    return scores


def _row_to_dict(row, score=None, engagement=None):
    return {
        "id": row.id,
        "source_id": row.source_id,
        "site": row.site,
        "uploader": row.uploader_key or row.uploader_name,
        "uploader_name": row.uploader_name,
        "post_id": row.post_id,
        "url": row.post_url,
        "posted_at": row.posted_at.isoformat() if row.posted_at else None,
        "title": row.title,
        "media_count": row.media_count or 0,
        "tags": json.loads(row.tags or "[]"),
        "likes": row.likes or 0,
        "retweets": row.retweets or 0,
        "replies": row.replies or 0,
        "quotes": row.quotes or 0,
        "bookmarks": row.bookmarks or 0,
        "views": row.views or 0,
        "engagement": round(float(engagement or 0.0), 2),
        "has_engagement": bool(row.has_engagement),
        "sensitive": bool(row.sensitive),
        "origin": row.origin,
        "score": round(score, 4) if score is not None else None,
        "thumbnail": "/api/posts/%d/thumbnail" % row.id,
    }


def _sort_recent(rows):
    return sorted(
        rows,
        key=lambda row: (row.posted_at is not None, row.posted_at or datetime.min),
        reverse=True,
    )


def section_recent(
    session, days=30, limit=24, site=None, include_sensitive=False, query=None, weights=None
):
    weights = weights or DEFAULT_WEIGHTS
    rows = _sort_recent(_window_rows(session, days, site, include_sensitive, query))
    return [
        _row_to_dict(row, engagement=_row_engagement(row, weights)) for row in rows[:limit]
    ]


def section_popular(
    session, days=30, limit=24, min_engagement=0, site=None,
    include_sensitive=False, query=None, weights=None,
):
    weights = weights or DEFAULT_WEIGHTS
    rows = [row for row in _window_rows(session, days, site, include_sensitive, query) if row.has_engagement]
    rows = [row for row in rows if _row_engagement(row, weights) >= float(min_engagement or 0)]
    scores = _normalized_scores(rows, weights)
    rows.sort(
        key=lambda row: (scores.get(row.id, 0.0), _row_engagement(row, weights)), reverse=True
    )
    return [
        _row_to_dict(row, scores.get(row.id, 0.0), _row_engagement(row, weights))
        for row in rows[:limit]
    ]


def section_rare_uploaders(
    session, days=30, limit=12, rare_max_posts=3, rare_min_gap_hours=168,
    site=None, include_sensitive=False, query=None, weights=None,
):
    weights = weights or DEFAULT_WEIGHTS
    rows = _window_rows(session, days, site, include_sensitive, query)
    grouped = {}
    for row in rows:
        key = (row.site or "", row.uploader_key or row.uploader_name or "?")
        grouped.setdefault(key, []).append(row)

    uploaders = []
    for (item_site, uploader), items in grouped.items():
        dates = sorted(item.posted_at for item in items if item.posted_at)
        gaps = [
            (later - earlier).total_seconds() / 3600.0
            for earlier, later in zip(dates, dates[1:])
        ]
        gap_median = statistics.median(gaps) if gaps else None
        posts_in_range = len(items)
        is_rare = posts_in_range <= int(rare_max_posts or 0) or (
            gap_median is not None and gap_median >= float(rare_min_gap_hours or 0)
        )
        best = max(items, key=lambda row: _row_engagement(row, weights))
        uploaders.append(
            {
                "site": item_site,
                "uploader": uploader,
                "name": max((row.uploader_name for row in items if row.uploader_name), default=uploader),
                "posts_in_range": posts_in_range,
                "gap_median_hours": round(gap_median, 1) if gap_median is not None else None,
                "is_rare": is_rare,
                "thumbnail": "/api/posts/%d/thumbnail" % best.id,
                "best": _row_to_dict(best, engagement=_row_engagement(best, weights)),
            }
        )

    # 희소도: 게시 수가 적을수록 + 중앙 간격이 길수록 높다.
    max_posts = max((item["posts_in_range"] for item in uploaders), default=1) or 1
    gaps = [item["gap_median_hours"] for item in uploaders if item["gap_median_hours"]]
    max_gap = max(gaps, default=1.0) or 1.0
    for item in uploaders:
        count_part = 1.0 - (item["posts_in_range"] / max_posts)
        gap_part = (item["gap_median_hours"] or 0.0) / max_gap
        item["rarity"] = round((count_part + gap_part) / 2.0, 4)
    uploaders.sort(key=lambda item: (item["rarity"], item["posts_in_range"] * -1), reverse=True)
    return uploaders[:limit]


def stats(session, days=30):
    total = session.execute(select(PostIndex)).scalars().all()
    rows = _window_rows(session, days)
    with_engagement = sum(1 for row in rows if row.has_engagement)
    sites = {}
    for row in rows:
        sites[row.site or "?"] = sites.get(row.site or "?", 0) + 1
    last = max((row.indexed_at for row in total if row.indexed_at), default=None)
    return {
        "indexed_posts": len(total),
        "window_posts": len(rows),
        "window_with_engagement": with_engagement,
        "indexed_sources": len({row.source_id for row in total if row.source_id}),
        "sites": dict(sorted(sites.items(), key=lambda kv: kv[1], reverse=True)),
        "last_indexed_at": last.isoformat() if last else None,
    }


def site_options(session):
    rows = session.execute(select(PostIndex.site).distinct()).scalars().all()
    return sorted({(value or "").strip() for value in rows if (value or "").strip()})
