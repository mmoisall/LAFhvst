# 구형 HVST → LAFhvst 이관 도구

기존 `lafhvst.db` 자료는 그대로 두고, 구형 프로젝트(`hvst/`)의 폴더/소스를 **추가**합니다.
중복(폴더=전체경로, 소스=정규화 site+key)은 자동으로 건너뜁니다.

- 루트 폴더 이름(절대경로)을 그대로 유지합니다.
- **폴더는 주기 디폴트(수동, `cyc_option=0`/`cyc=1`)로 이관**됩니다.
- **열/행 제외 선택** 지원: 특정 열(config/memo/custom_fld 등)이나 특정 행(폴더/소스)을 골라 이관에서 제외.
- `loc_alt`/`loc_cmb`/`cyc_min`/`cyc_max`/`ignore_level` 등 폐기 필드는 이관하지 않습니다.
- 자격증명(cookies/토큰/비밀번호)은 **기본 제외**됩니다.
- 실행 전 `lafhvst.db` 백업을 권장합니다.

## 실행

### UI (권장)
```
python tools/hvst_import/app.py
```
경로 자동 채움 → **미리보기** → **이관 실행**.

### CLI
```
python tools/import_hvst.py --dry-run          # 미리보기(기본)
python tools/import_hvst.py --apply            # 실제 이관
python tools/import_hvst.py --apply --include-secrets
python tools/import_hvst.py --db "<경로>" --settings "<경로>" --apply
python tools/import_hvst.py --apply --exclude-columns "config,memo"
python tools/import_hvst.py --apply --exclude-rows "source:12,folder:3"
```

UI에서는 미리보기 후 **제외할 열** 체크, **제외할 행** 목록 선택이 가능합니다.
폴더는 주기 디폴트(수동)로 이관됩니다.

## 매핑 요약

| 구형 | 최신 | 비고 |
|---|---|---|
| folder.name | folders.name | 루트 절대경로 유지 |
| folder.parent_id | folders.parent_id | ID 재매핑 |
| folder.log_level | folders.log_level | 없으면 INFO |
| source.parent_id | sources.folder_id | 재매핑 |
| source.site/key | sources.site/key | 사이트 정규화 |
| source.cyc(분) | sources.cyc_option=1, cyc=ceil(cyc/60) | 최소 1h |
| source.check_point/end_point | recent_run_at / recent_file_date | 참고 |
| source.custom_fld | memo에 보존 | |
| source.config | sources.config | 자격증명 제거(옵션 시 포함) |
