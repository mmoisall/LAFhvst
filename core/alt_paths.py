"""대체경로(alt/cmb) 동기화 엔진.

- alt: 원본 폴더 구조를 유지하며 하드링크(기본) 또는 일반 복사로 대체경로에 반영.
- cmb: 하위폴더 내용물을 대체경로 최상위로 평탄화 병합.

원본 = 아이템(소스/폴더)의 실제 디렉터리 전체(사용자가 수동으로 추가한 파일 포함).
동기화는 증분 방식으로 신규/변경 파일만 처리한다.
"""

import hashlib
import os
import shutil

from . import utils

ALT_MODES = ("hardlink", "copy")
ALT_SCHEDULES = ("n_hours", "on_run", "manual")
_READ_CHUNK = 1024 * 1024


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item or "").strip()]
    text = str(value).strip()
    return [text] if text else []


def resolve_targets(config, base_dir, kind):
    """대체경로 대상 디렉터리 목록을 계산한다(기본값 = base_dir_alt / base_dir_cmb)."""
    config = config or {}
    key = "alt_paths" if kind == "alt" else "cmb_paths"
    raw = _as_list(config.get(key))
    suffix = "_alt" if kind == "alt" else "_cmb"
    if not raw:
        return [os.path.normpath(base_dir.rstrip("\\/") + suffix)]
    result = []
    for item in raw:
        text = item.strip()
        if not text or text.lower() == "default":
            text = base_dir.rstrip("\\/") + suffix
        if not os.path.isabs(text):
            text = os.path.join(base_dir, text)
        result.append(os.path.normpath(text))
    # dedupe, 원본과 동일한 대상 제외
    out = []
    base_norm = os.path.normpath(base_dir)
    for path in result:
        if path == base_norm or path in out:
            continue
        out.append(path)
    return out


def _file_signature(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return {"size": stat.st_size, "mtime": stat.st_mtime}


def _same_content(path_a, path_b):
    try:
        if os.path.getsize(path_a) != os.path.getsize(path_b):
            return False
    except OSError:
        return False
    try:
        with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
            while True:
                chunk_a = fa.read(_READ_CHUNK)
                chunk_b = fb.read(_READ_CHUNK)
                if chunk_a != chunk_b:
                    return False
                if not chunk_a:
                    return True
    except OSError:
        return False


def _link_or_copy(src, dst, mode):
    """하드링크 시도, 불가하면 일반 복사로 폴백. 반환: 'link'|'copy'."""
    utils.ensure_dir(os.path.dirname(dst) or ".")
    if mode == "hardlink":
        try:
            if os.path.exists(dst):
                os.remove(dst)
            os.link(src, dst)
            return "link"
        except OSError:
            pass
    shutil.copy2(src, dst)
    return "copy"


def _iter_files(root):
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != utils.METADATA_DIRNAME]
        for name in files:
            lowered = name.lower()
            if lowered.endswith(".part") or lowered.endswith(".json.tmp"):
                continue
            if lowered.endswith(".json") or lowered.endswith(".yaml") or lowered.endswith(".yml"):
                continue
            path = os.path.join(base, name)
            yield path, os.path.relpath(path, root)


def _unique_flat_name(target_dir, rel_path, src_path):
    """cmb 평탄화 충돌 처리: 내용 동일→오래된 것 유지, 상이→접두어."""
    name = os.path.basename(rel_path)
    dst = os.path.join(target_dir, name)
    if not os.path.exists(dst):
        return dst, None
    if _same_content(src_path, dst):
        # 동일 내용: 더 오래된 파일만 유지
        try:
            if os.path.getmtime(src_path) < os.path.getmtime(dst):
                os.remove(dst)
                return dst, "replace_older"
            return dst, "keep_existing"
        except OSError:
            return dst, "keep_existing"
    # 내용 상이 → 상위 폴더명 접두어
    parent = os.path.basename(os.path.dirname(rel_path)) or "root"
    candidate = os.path.join(target_dir, utils.sanitize_component(parent, 40) + "__" + name)
    counter = 1
    while os.path.exists(candidate):
        if _same_content(src_path, candidate):
            return candidate, "keep_existing"
        candidate = os.path.join(
            target_dir,
            utils.sanitize_component(parent, 40) + "__" + str(counter) + "__" + name,
        )
        counter += 1
    return candidate, "prefix"


def sync(base_dir, config, kind, force=False):
    """대체경로 동기화. 반환: 리포트 dict."""
    config = config or {}
    mode = str(config.get("alt_mode") or "hardlink").lower()
    if mode not in ALT_MODES:
        mode = "hardlink"
    targets = resolve_targets(config, base_dir, kind)
    report = {
        "kind": kind,
        "mode": mode,
        "base_dir": base_dir,
        "targets": targets,
        "linked": 0,
        "copied": 0,
        "skipped": 0,
        "conflicts": 0,
        "errors": [],
        "total_source_files": 0,
    }
    if not base_dir or not os.path.isdir(base_dir):
        report["errors"].append("원본 폴더가 없습니다: " + str(base_dir))
        return report
    if not targets:
        report["errors"].append("대체경로 대상이 없습니다.")
        return report

    source_files = list(_iter_files(base_dir))
    report["total_source_files"] = len(source_files)
    base_norm = os.path.normpath(base_dir)

    for target in targets:
        # 대상이 원본의 하위/동일이면 순환 위험 → 제외
        target_norm = os.path.normpath(target)
        if target_norm == base_norm or target_norm.startswith(base_norm + os.sep):
            report["errors"].append("원본 하위 경로는 대상이 될 수 없습니다: " + target)
            continue
        utils.ensure_dir(target)
        for src_path, rel_path in source_files:
            try:
                if kind == "cmb":
                    dst, action = _unique_flat_name(target, rel_path, src_path)
                    if action == "keep_existing":
                        report["skipped"] += 1
                        continue
                    if action == "replace_older":
                        report["conflicts"] += 1
                    if action == "prefix":
                        report["conflicts"] += 1
                else:
                    dst = os.path.join(target, rel_path)
                    if os.path.exists(dst):
                        sig_src = _file_signature(src_path)
                        sig_dst = _file_signature(dst)
                        if sig_src and sig_dst and sig_src["mtime"] <= sig_dst["mtime"]:
                            report["skipped"] += 1
                            continue
                result = _link_or_copy(src_path, dst, mode)
                if result == "link":
                    report["linked"] += 1
                else:
                    report["copied"] += 1
            except OSError as exc:
                report["errors"].append("%s: %s" % (src_path, exc))
    return report


def prune(base_dir, config, kind):
    """원본에 없는 대체경로 파일을 제거한다(수동 트리거 전용)."""
    config = config or {}
    targets = resolve_targets(config, base_dir, kind)
    report = {"kind": kind, "removed": 0, "errors": []}
    if not os.path.isdir(base_dir):
        report["errors"].append("원본 폴더가 없습니다.")
        return report
    source_names = set()
    if kind == "cmb":
        for _path, rel in _iter_files(base_dir):
            source_names.add(os.path.basename(rel))
    else:
        for _path, rel in _iter_files(base_dir):
            source_names.add(rel.lower())
    for target in targets:
        if not os.path.isdir(target):
            continue
        for path, rel in _iter_files(target):
            exists = (os.path.basename(rel) in source_names) if kind == "cmb" else (rel.lower() in source_names)
            if not exists:
                try:
                    os.remove(path)
                    report["removed"] += 1
                except OSError as exc:
                    report["errors"].append("%s: %s" % (path, exc))
    return report
