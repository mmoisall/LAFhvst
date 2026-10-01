"""구형 HVST → LAFhvst 이관 엔진.

구형 `hvst.db`(folder/source 단일 테이블 트리)를 현재 `lafhvst.db`(folders/sources)
로 이관한다. 기존 자료는 보존하고 중복만 건너뛴다.

- 루트 폴더 이름(절대경로)을 그대로 유지한다.
- 자격증명(cookies/refresh-token/api-key/password 등)은 기본 제외.
- loc_alt/loc_cmb/cyc_min/cyc_max/ignore_level 등 폐기 필드는 이관하지 않는다.
- 중복: 폴더=전체경로, 소스=(정규화 site, key) [없으면 url].
"""

import argparse
import json
import math
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import models, site_url  # noqa: E402
from core.database import get_session, init_db  # noqa: E402

DEFAULT_DB = os.environ.get("HVST_DB", "")
DEFAULT_SETTINGS = os.environ.get("HVST_SETTINGS", "")


def _resolve_db_path(db_path):
    """구형 hvst.db 경로를 결정한다(인자 > HVST_DB 환경변수). 없으면 명확한 오류."""
    path = (db_path or DEFAULT_DB or "").strip()
    if not path:
        raise FileNotFoundError(
            "구형 hvst.db 경로가 필요합니다. --db 옵션 또는 HVST_DB 환경변수로 지정하세요."
        )
    return path

# 자격증명으로 간주해 기본 제거하는 config 키
SECRET_KEYS = {
    "cookies",
    "auth_token",
    "refresh-token",
    "refresh_token",
    "api-key",
    "api_key",
    "api-secret",
    "api_secret",
    "client-id",
    "client_id",
    "client-secret",
    "client_secret",
    "password",
    "passwd",
    "username",
    "user",
    "access_token",
    "token",
}

_SITE_NORMALIZE = {
    "kemono.cr": "kemono",
    "kemono.su": "kemono",
    "kemono.party": "kemono",
    "baraag.net": "baraag",
    "rule34.paheal.net": "rule34.paheal",
    "sankakucomplex": "sankaku",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "bsky.app": "bluesky",
}


def normalize_site(site) -> str | None:
    name = (site or "").strip()
    if not name:
        return None
    lowered = name.lower()
    if lowered in _SITE_NORMALIZE:
        return _SITE_NORMALIZE[lowered]
    return site_url.normalize_site(lowered) or lowered


def _strip_secrets(obj):
    """config/dict 에서 자격증명 키를 재귀적으로 제거."""
    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            if str(key).lower() in SECRET_KEYS:
                continue
            result[key] = _strip_secrets(value)
        return result
    if isinstance(obj, list):
        return [_strip_secrets(item) for item in obj]
    return obj


def _load_json_field(raw):
    if not raw:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed
    except (TypeError, ValueError):
        return {"_raw": text}


def _topo_order(folders):
    """부모가 항상 자식보다 먼저 오도록 정렬 (id 순서가 꼬여도 안전)."""
    by_id = {row["id"]: row for row in folders}
    ordered = []
    visiting = set()
    done = set()

    def visit(fid):
        if fid in done or fid not in by_id:
            return
        if fid in visiting:
            return
        visiting.add(fid)
        parent = by_id[fid].get("parent_id")
        if parent is not None and parent in by_id:
            visit(parent)
        visiting.discard(fid)
        done.add(fid)
        ordered.append(by_id[fid])

    for row in folders:
        visit(row["id"])
    return ordered


def _folder_paths(folders):
    """구형 폴더 id -> 전체 경로(대소문자 무시) 매핑."""
    by_id = {row["id"]: row for row in folders}
    cache: dict[int, str] = {}

    def build(fid):
        if fid in cache:
            return cache[fid]
        row = by_id.get(fid)
        if row is None:
            return ""
        parent = row.get("parent_id")
        name = (row.get("name") or "").strip()
        if parent is None or parent not in by_id:
            path = name
        else:
            base = build(parent)
            path = (base + "/" + name) if base else name
        cache[fid] = path
        return path

    result = {}
    for row in folders:
        result[row["id"]] = build(row["id"]).replace("\\", "/").lower()
    return result, by_id


def _read_old_db(db_path):
    if not os.path.isfile(db_path):
        raise FileNotFoundError("구형 DB를 찾을 수 없습니다: " + db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        folders = [dict(r) for r in conn.execute("SELECT * FROM folder ORDER BY id")]
        sources = [dict(r) for r in conn.execute("SELECT * FROM source ORDER BY id")]
        return folders, sources
    finally:
        conn.close()


def _read_settings(path):
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                return json.load(handle)
        except Exception:
            return {}


def _existing_state(session):
    """현재 DB의 중복 판정용 집합."""
    folder_paths = set()
    for folder in session.execute(models.select(models.Folder)).scalars().all():
        chain = models.folder_chain(session, folder.id)
        path = "/".join((item.name or "") for item in chain).replace("\\", "/").lower()
        folder_paths.add(path)
    source_keys = set()
    for source in session.execute(models.select(models.Source)).scalars().all():
        site = normalize_site(source.site)
        key = (source.key or "").strip().lower()
        if key:
            source_keys.add((site, key))
        else:
            source_keys.add(("__url__", (source.url or "").strip().lower()))
    return folder_paths, source_keys


def inspect(db_path=DEFAULT_DB, settings_path=DEFAULT_SETTINGS) -> dict:
    db_path = _resolve_db_path(db_path)
    folders, sources = _read_old_db(db_path)
    settings = _read_settings(settings_path)
    sites: dict[str, int] = {}
    for src in sources:
        name = normalize_site(src.get("site")) or "(없음)"
        sites[name] = sites.get(name, 0) + 1
    return {
        "db_path": os.path.abspath(db_path),
        "settings_path": os.path.abspath(settings_path) if settings_path and os.path.isfile(settings_path) else None,
        "folders": len(folders),
        "sources": len(sources),
        "config_count": sum(1 for s in sources if s.get("config")),
        "custom_field_count": sum(1 for s in sources if s.get("custom_fld")),
        "sites": dict(sorted(sites.items(), key=lambda kv: kv[1], reverse=True)),
        "has_config_default": bool(settings.get("config_default")),
        "save_path": settings.get("save_path"),
    }


def _convert_source_config(raw_config, settings, include_secrets):
    config = _load_json_field(raw_config)
    if include_secrets and settings.get("config_default"):
        merged = dict(settings.get("config_default") or {})
        merged.update(config or {})
        config = merged
    if not include_secrets:
        config = _strip_secrets(config)
    return config


def _columns_for(kind):
    """구형 DB kind(folder/source)의 열 목록(이관 시 열 제외 선택용)."""
    if kind == "folder":
        return [
            "name",
            "parent_id",
            "log_level",
            "config",
            "memo",
        ]
    return [
        "url",
        "site",
        "key",
        "cyc",
        "log_level",
        "config",
        "memo",
        "custom_fld",
        "check_point",
        "end_point",
    ]


def _convert_source_payload(src, new_folder_id, settings, include_secrets, exclude_columns=None):
    exclude_columns = set(exclude_columns or [])
    cyc_minutes = src.get("cyc") or 0
    try:
        cyc_hours = max(1, int(math.ceil(float(cyc_minutes) / 60.0))) if cyc_minutes else 1
    except (TypeError, ValueError):
        cyc_hours = 1
    want_config = "config" not in exclude_columns
    want_custom = "custom_fld" not in exclude_columns
    want_memo = "memo" not in exclude_columns
    want_cyc = "cyc" not in exclude_columns
    want_log = "log_level" not in exclude_columns
    want_points = "check_point" not in exclude_columns or "end_point" not in exclude_columns

    config = _convert_source_config(src.get("config"), settings, include_secrets) if want_config else {}
    memo = (src.get("memo") or None) if want_memo else None
    if want_custom:
        custom = _load_json_field(src.get("custom_fld"))
        if custom:
            extra = "custom_fld: " + json.dumps(custom, ensure_ascii=False)
            memo = (memo + "\n" + extra) if memo else extra
    payload = {
        "name": src.get("url") or "",
        "url": src.get("url") or "",
        "folder_id": new_folder_id,
        "site": normalize_site(src.get("site")),
        "key": src.get("key"),
        "config": config,
        "cyc_option": 1 if want_cyc else 0,
        "cyc": cyc_hours if want_cyc else 1,
        "log_level": (src.get("log_level") or "INFO") if want_log else "INFO",
        "memo": memo,
    }
    if want_points and src.get("check_point"):
        payload["recent_run_at"] = src.get("check_point")
    if want_points and src.get("end_point"):
        payload["recent_file_date"] = src.get("end_point")
    return payload


def build_plan(
    db_path=DEFAULT_DB,
    settings_path=DEFAULT_SETTINGS,
    include_secrets=False,
    exclude_columns=None,
    exclude_row_ids=None,
) -> dict:
    db_path = _resolve_db_path(db_path)
    settings_path = settings_path or DEFAULT_SETTINGS
    folders, sources = _read_old_db(db_path)
    settings = _read_settings(settings_path)
    exclude_columns = set(exclude_columns or [])
    exclude_row_ids = {str(value) for value in (exclude_row_ids or [])}
    session = get_session()
    try:
        existing_folders, existing_sources = _existing_state(session)
    finally:
        session.close()

    old_paths, by_id = _folder_paths(folders)
    folder_plan = []
    planned_folder_paths = set(existing_folders)
    for folder in folders:
        path = old_paths.get(folder["id"], "").lower()
        dup = path in existing_folders
        row_key = "folder:%s" % folder["id"]
        excluded = row_key in exclude_row_ids or str(folder["id"]) in exclude_row_ids
        folder_plan.append({
            "old_id": folder["id"],
            "name": folder.get("name"),
            "path": old_paths.get(folder["id"]),
            "parent_id": folder.get("parent_id"),
            "log_level": folder.get("log_level"),
            "duplicate": dup,
            "excluded": excluded,
        })
        planned_folder_paths.add(path)

    source_plan = []
    seen = set()
    for src in sources:
        site = normalize_site(src.get("site"))
        key = (src.get("key") or "").strip().lower()
        dedupe_key = (site, key) if key else ("__url__", (src.get("url") or "").strip().lower())
        dup = dedupe_key in existing_sources or dedupe_key in seen
        seen.add(dedupe_key)
        row_key = "source:%s" % src["id"]
        excluded = row_key in exclude_row_ids or str(src["id"]) in exclude_row_ids
        source_plan.append({
            "old_id": src["id"],
            "url": src.get("url"),
            "site": site,
            "key": src.get("key"),
            "parent_id": src.get("parent_id"),
            "cyc_minutes": src.get("cyc"),
            "cyc_hours": _convert_source_payload(
                src, None, settings, include_secrets, exclude_columns
            )["cyc"],
            "has_config": bool(src.get("config")) and "config" not in exclude_columns,
            "duplicate": dup,
            "excluded": excluded,
        })

    new_folders = [f for f in folder_plan if not f["duplicate"] and not f["excluded"]]
    new_sources = [s for s in source_plan if not s["duplicate"] and not s["excluded"]]
    excluded_folders = [f for f in folder_plan if f["excluded"]]
    excluded_sources = [s for s in source_plan if s["excluded"]]
    return {
        "inspect": inspect(db_path, settings_path),
        "include_secrets": bool(include_secrets),
        "exclude_columns": sorted(exclude_columns),
        "exclude_row_ids": sorted(exclude_row_ids),
        "columns": {"folder": _columns_for("folder"), "source": _columns_for("source")},
        "folders": folder_plan,
        "sources": source_plan,
        "summary": {
            "folders_total": len(folder_plan),
            "folders_new": len(new_folders),
            "folders_duplicate": len(folder_plan) - len(new_folders),
            "sources_total": len(source_plan),
            "sources_new": len(new_sources),
            "sources_duplicate": len(source_plan) - len(new_sources),
            "folders_excluded": len(excluded_folders),
            "sources_excluded": len(excluded_sources),
        },
    }


def _folder_path_of(session, folder_id):
    chain = models.folder_chain(session, folder_id)
    return "/".join((item.name or "") for item in chain).replace("\\", "/").lower()


def migrate(
    db_path=DEFAULT_DB,
    settings_path=DEFAULT_SETTINGS,
    include_secrets=False,
    dry_run=False,
    exclude_columns=None,
    exclude_row_ids=None,
) -> dict:
    db_path = _resolve_db_path(db_path)
    settings_path = settings_path or DEFAULT_SETTINGS
    init_db()
    folders, sources = _read_old_db(db_path)
    settings = _read_settings(settings_path)
    old_paths, by_id = _folder_paths(folders)
    exclude_columns = set(exclude_columns or [])
    exclude_row_ids = {str(value) for value in (exclude_row_ids or [])}

    def _row_excluded(kind, old_id):
        return ("%s:%s" % (kind, old_id)) in exclude_row_ids or str(old_id) in exclude_row_ids

    report = {
        "dry_run": bool(dry_run),
        "include_secrets": bool(include_secrets),
        "exclude_columns": sorted(exclude_columns),
        "exclude_row_ids": sorted(exclude_row_ids),
        "folders_created": 0,
        "folders_skipped": 0,
        "folders_excluded": 0,
        "sources_created": 0,
        "sources_skipped": 0,
        "sources_excluded": 0,
        "warnings": [],
        "id_map": {},
    }

    session = get_session()
    try:
        existing_folders, existing_sources = _existing_state(session)

        # 1) 폴더 (부모 우선 정렬: id 순서가 꼬여도 안전)
        id_map: dict[int, int] = {}
        excluded_folders: set[int] = set()
        for folder in _topo_order(folders):
            old_id = folder["id"]
            if _row_excluded("folder", old_id):
                report["folders_excluded"] += 1
                excluded_folders.add(old_id)
                continue
            # 제외된 부모의 하위는 함께 제외
            if folder.get("parent_id") in excluded_folders:
                report["folders_excluded"] += 1
                excluded_folders.add(old_id)
                continue
            path = old_paths.get(old_id, "").lower()
            if path in existing_folders:
                report["folders_skipped"] += 1
                # 기존 폴더 id 매핑 확보
                matched = _find_existing_folder(session, path)
                if matched is not None:
                    id_map[old_id] = matched
                continue
            parent_old = folder.get("parent_id")
            new_parent = None
            if parent_old is not None:
                new_parent = id_map.get(parent_old)
                if new_parent is None:
                    parent_path = old_paths.get(parent_old, "").lower()
                    new_parent = _find_existing_folder(session, parent_path)
                    if new_parent is None:
                        report["warnings"].append(
                            "부모 폴더를 찾지 못함: %s (부모 old_id=%s)" % (folder.get("name"), parent_old)
                        )
            payload = {
                "name": folder.get("name") or "New Folder",
                "parent_id": new_parent,
                "log_level": (folder.get("log_level") or "INFO")
                if "log_level" not in exclude_columns else "INFO",
                "memo": (folder.get("memo") if "memo" not in exclude_columns else None),
                "cyc_option": 0,
                "cyc": 1,
            }
            if "config" not in exclude_columns and folder.get("config"):
                config = _strip_secrets(_load_json_field(folder.get("config")))
                if config:
                    payload["config"] = config
            if dry_run:
                report["folders_created"] += 1
                id_map[old_id] = -old_id
                continue
            created = models.create_folder(session, payload)
            id_map[old_id] = created["id"]
            report["folders_created"] += 1
        report["id_map"] = {str(k): v for k, v in id_map.items()}

        # 2) 소스
        seen_sources = set()
        for src in sources:
            old_id = src["id"]
            if _row_excluded("source", old_id) or src.get("parent_id") in excluded_folders:
                report["sources_excluded"] += 1
                continue
            site = normalize_site(src.get("site"))
            key = (src.get("key") or "").strip().lower()
            dedupe_key = (site, key) if key else ("__url__", (src.get("url") or "").strip().lower())
            if dedupe_key in existing_sources or dedupe_key in seen_sources:
                report["sources_skipped"] += 1
                continue
            seen_sources.add(dedupe_key)
            parent_old = src.get("parent_id")
            new_folder_id = id_map.get(parent_old)
            if new_folder_id is None:
                parent_path = old_paths.get(parent_old, "").lower()
                new_folder_id = _find_existing_folder(session, parent_path)
            payload = _convert_source_payload(
                src, new_folder_id, settings, include_secrets, exclude_columns
            )
            if dry_run:
                report["sources_created"] += 1
                continue
            models.create_source(session, payload)
            report["sources_created"] += 1

        if dry_run:
            session.rollback()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return report


def _find_existing_folder(session, path):
    if not path:
        return None
    for folder in session.execute(models.select(models.Folder)).scalars().all():
        if _folder_path_of(session, folder.id) == path:
            return folder.id
    return None


def run_cli(argv=None) -> int:
    parser = argparse.ArgumentParser(description="구형 HVST → LAFhvst 이관 도구")
    parser.add_argument("--db", default=DEFAULT_DB, help="구형 hvst.db 경로 (또는 HVST_DB 환경변수)")
    parser.add_argument("--settings", default=DEFAULT_SETTINGS, help="구형 hvst_setting.json 경로 (또는 HVST_SETTINGS 환경변수)")
    parser.add_argument("--include-secrets", action="store_true", help="자격증명 포함(주의)")
    parser.add_argument("--apply", action="store_true", help="실제 이관 (기본은 미리보기)")
    parser.add_argument("--dry-run", action="store_true", help="미리보기만")
    parser.add_argument("--json", action="store_true", help="JSON 출력")
    parser.add_argument("--exclude-columns", default="", help="제외할 열(쉼표 구분)")
    parser.add_argument("--exclude-rows", default="", help="제외할 행 old_id (쉼표 구분, folder:1/source:2 형식 가능)")
    args = parser.parse_args(argv)

    try:
        args.db = _resolve_db_path(args.db)
    except FileNotFoundError as exc:
        parser.error(str(exc))

    exclude_columns = [c.strip() for c in (args.exclude_columns or "").split(",") if c.strip()]
    exclude_row_ids = [r.strip() for r in (args.exclude_rows or "").split(",") if r.strip()]

    do_apply = args.apply and not args.dry_run
    if not do_apply:
        plan = build_plan(
            args.db, args.settings, args.include_secrets, exclude_columns, exclude_row_ids
        )
        if args.json:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
        else:
            info = plan["inspect"]
            sm = plan["summary"]
            print("=== 구형 HVST 미리보기 ===")
            print("DB:", info["db_path"])
            print("폴더: %d (신규 %d / 중복 %d)" % (info["folders"], sm["folders_new"], sm["folders_duplicate"]))
            print("소스: %d (신규 %d / 중복 %d)" % (info["sources"], sm["sources_new"], sm["sources_duplicate"]))
            print("사이트:", ", ".join("%s=%d" % kv for kv in info["sites"].items()))
            print("자격증명 포함:", plan["include_secrets"])
            print("제외 열:", ", ".join(plan["exclude_columns"]) or "(없음)")
            print("제외 행:", ", ".join(plan["exclude_row_ids"]) or "(없음)")
            print("폴더 주기: 디폴트(수동) 적용")
            print("\n실제 이관하려면 --apply 를 사용하세요.")
        return 0

    report = migrate(
        args.db, args.settings, args.include_secrets, dry_run=False,
        exclude_columns=exclude_columns, exclude_row_ids=exclude_row_ids,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("=== 이관 완료 ===")
        print("폴더 생성 %d, 스킵 %d" % (report["folders_created"], report["folders_skipped"]))
        print("소스 생성 %d, 스킵 %d" % (report["sources_created"], report["sources_skipped"]))
        for warning in report["warnings"]:
            print("경고:", warning)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
