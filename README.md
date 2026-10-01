# LAFhvst

FastAPI + pywebview 기반의 소셜 미디어 아카이빙 엔진. [gallery-dl](https://github.com/mikf/gallery-dl)을
백엔드로 사용해 여러 사이트의 미디어를 주기적으로 수집하고, 폴더/소스 트리, 프로필, 스케줄러,
감시(watcher), 로그, 대체경로 동기화를 하나의 로컬 웹 UI에서 관리한다.

## 주요 기능

- **소스/폴더 관리**: 사이트·키 기반 소스와 이를 묶는 폴더를 트리로 관리
- **스케줄러**: 주기 실행, 사이클, 재학습(relearn) 기반 업로드 주기 추정
- **프로필**: 사이트별 로그인 세션(Playwright persistent context) 관리 및 프로필 그룹
- **Watcher**: 사이트 세션을 주기적으로 새로고침하며 이벤트 수집
- **Reactive Rules**: 키워드 패턴 기반 자동 분류 규칙
- **대체경로(alt/cmb)**: 원본 폴더를 하드링크/복사로 동기화, 평탄 병합, prune
- **로그/오류 분석**: gallery-dl 오류 로그 수집·분류·해결 상태 관리
- **탐색기(Explorer)**: 서버 파일 탐색, 배치 이동/편집/삭제/실행, 썸네일
- **HVST 이관**: 구형 `hvst.db` → `lafhvst.db` 이관 도구는 별도 저장소 [mmoisall/LAF](https://github.com/mmoisall/LAF) 참고

## 요구 사항

- Windows (pywebview 기반 데스크톱 창), Python 3.11+
- 의존성: `requirements.txt` 참고 (FastAPI, uvicorn, pywebview, SQLAlchemy, APScheduler, gallery-dl, playwright, Pillow)

## 설치

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

## 실행

```bash
python main.py            # 데스크톱 창 + 로컬 서버 (기본 포트 17363)
python main.py --serve    # 창 없이 서버만 실행 (또는 LAF_NO_GUI=1)
```

실행하면 로컬(`http://127.0.0.1:17363`) 및 동일 네트워크 주소가 콘솔에 표시된다.

## 데이터 및 설정

최초 실행 시 아래가 자동 생성된다.

| 경로 | 설명 |
| --- | --- |
| `lafhvst.db` | SQLite 데이터베이스 (계정/프로필/소스/로그) |
| `data/gallery-dl.conf` | 앱 전용 gallery-dl 설정 (전역 설정을 시크릿 제거 후 seed) |
| `data/profiles/` | Playwright 로그인 프로필 (쿠키/세션 포함) |
| `data/watcher/` | 사이트별 세션 감시용 디렉터리 |
| `data/thumbnails/` | 썸네일 캐시 |
| `log/` | gallery-dl 로그 |

`gallery-dl.conf.example`를 `data/gallery-dl.conf`로 복사하면 사이트별 저장 폴더/파일명 규칙을
미리 지정할 수 있다. 복사하지 않아도 최초 실행 시 자동 생성된다.

> `data/`, `log/`, `*.db*`, 다운로드 결과물은 `.gitignore`로 제외된다. 쿠키·세션 등 자격증명이
> 포함되므로 절대 커밋하지 않는다.

## 구형 HVST 이관 (별도 저장소)

구형 HVST(=`LAFhvst old`)의 `hvst.db` 를 현재 `lafhvst.db` 로 이관하는 도구는
별도 저장소 **mmoisall/LAF** 에서 관리한다.

- 저장소: https://github.com/mmoisall/LAF (`hvst_import/`)
- 사용: `python import_hvst.py --lafhvst "<LAFhvst 경로>" --db "<hvst.db 경로>" --apply`

## 디렉터리 구조

```
LAFhvst/
├─ main.py            # 진입점 (서버 스레드 + pywebview 창)
├─ server.py          # FastAPI 앱 및 REST API
├─ scheduler.py       # APScheduler 기반 스케줄링
├─ requirements.txt
├─ core/              # 도메인 로직
│  ├─ models.py       # SQLAlchemy 모델
│  ├─ database.py     # 엔진/세션/마이그레이션
│  ├─ gdl_executor.py # gallery-dl 실행/설정
│  ├─ browser_manager.py / browser_watcher.py
│  ├─ alt_paths.py    # 대체경로 동기화
│  ├─ kde.py, sites.py, site_url.py, utils.py, error_logger.py
└─ frontend/          # 정적 웹 UI (HTML/CSS/JS)
```

> 구형 HVST 이관 도구는 별도 저장소 [mmoisall/LAF](https://github.com/mmoisall/LAF) 에 있습니다.

## 라이선스

[MIT](LICENSE) © 2026 mmoisall
