# LAFhvst

<a id="english"></a>
**English** | [한국어](#korean)

---

## English

A social media archiving engine built on FastAPI + pywebview. It uses
[gallery-dl](https://github.com/mikf/gallery-dl) as a backend to periodically collect media from
multiple sites, and manages the folder/source tree, profiles, scheduler, watcher, logs, and
alternate-path sync from a single local web UI.

### Features

- **Source / Folder management**: manage site+key sources and the folders that group them in a tree
- **Scheduler**: periodic runs, cycles, relearn-based upload-cycle estimation
- **Profiles**: per-site login sessions (Playwright persistent context) and profile groups
- **Watcher**: periodically refreshes site sessions and collects events
- **Reactive Rules**: keyword-pattern based auto-classification rules
- **Alternate paths (alt/cmb)**: sync original folders via hardlink/copy, flat merge, prune
- **Log / error analysis**: collect, classify, and track resolution of gallery-dl error logs
- **Explorer**: server file browsing, batch move/edit/delete/run, thumbnails
- **HVST migration**: the old `hvst.db` → `lafhvst.db` tool lives in a separate repo
  [mmoisall/LAF](https://github.com/mmoisall/LAF)

### Requirements

- Windows (pywebview desktop window), Python 3.11+
- Dependencies: see `requirements.txt` (FastAPI, uvicorn, pywebview, SQLAlchemy, APScheduler,
  gallery-dl, playwright, Pillow)

### Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### Run

```bash
python main.py            # desktop window + local server (default port 17363)
python main.py --serve    # server only, no window (or LAF_NO_GUI=1)
```

On start, the local (`http://127.0.0.1:17363`) and same-network addresses are printed to the console.

### EXE (Releases)

Download `LAFhvst-<version>-win64.zip` from
[Releases](https://github.com/mmoisall/LAFhvst/releases), unzip and run. Data (`lafhvst.db`,
`data/`, `log/`) is created next to the exe, so you can move/back up the whole folder.

| Executable | Description |
| --- | --- |
| `LAFhvst.exe` | Desktop window (GUI). Tray support |
| `LAFhvst-server.exe` | Windowless server (console). Shows banner/logs, exit with `Ctrl+C` |
| `gallery-dl.exe` | Bundled gallery-dl (used automatically) |

- Server port: `--port 17363` or the `LAF_PORT` env var
- Requirements: Windows 10/11, WebView2 runtime (preinstalled)
- Profile login / watcher: system Chrome or Edge
- Pixiv ugoira (webm) conversion: system ffmpeg (only that step is skipped if missing)

#### Tray / Server mode

- Window **X (close)** → hides to tray instead of quitting; the server keeps running
- Home top-right **"Server mode"** button → same (hide to tray)
- Tray icon **right-click** → `Open` / `Server address` / `Quit`
- Fully quit via the tray menu **Quit**
- Settings → "Close to tray (keep server)" toggles this behavior (off = X quits)

Build from source:

```powershell
./build.ps1 -Version 0.1.2
# output: dist/LAFhvst-0.1.2-win64.zip
```

### Updates

On startup the app checks GitHub Releases for a new version. See **Settings → Updates**:

- **Check for updates on start** (on by default)
- **Auto update (download & install)**: automatically download, install, and restart
  (packaged exe only)
- **Check now** button

When a new version exists, a banner appears on the Home screen and can be applied with
**Install update**. The update quits the app, replaces files, and restarts; data (`data/`,
`lafhvst.db`, `log/`) is preserved. When running from source, only the check works and install is
disabled.

### Data & settings

Created automatically on first run.

| Path | Description |
| --- | --- |
| `lafhvst.db` | SQLite database (accounts / profiles / sources / logs) |
| `data/gallery-dl.conf` | App-specific gallery-dl config (seeded from global config, secrets stripped) |
| `data/profiles/` | Playwright login profiles (contains cookies/sessions) |
| `data/watcher/` | Per-site session watch directory |
| `data/thumbnails/` | Thumbnail cache |
| `log/` | gallery-dl logs |

Copy `gallery-dl.conf.example` to `data/gallery-dl.conf` to preset per-site directory/filename
rules. It is auto-created on first run even if you don't copy it.

> `data/`, `log/`, `*.db*`, and downloaded media are excluded via `.gitignore`. They contain
> credentials (cookies/sessions) — never commit them.

### Legacy HVST migration (separate repo)

The tool that migrates the legacy HVST (`LAFhvst old`) `hvst.db` to the current `lafhvst.db` is
maintained in a separate repository **mmoisall/LAF**.

- Repo: https://github.com/mmoisall/LAF (`hvst_import/`)
- Usage: `python import_hvst.py --lafhvst "<path to LAFhvst>" --db "<path to hvst.db>" --apply`

### Directory structure

```
LAFhvst/
├─ main.py            # entry point (server thread + pywebview window)
├─ server.py          # FastAPI app and REST API
├─ scheduler.py       # APScheduler-based scheduling
├─ requirements.txt
├─ core/              # domain logic
│  ├─ models.py       # SQLAlchemy models
│  ├─ database.py     # engine / session / migrations
│  ├─ gdl_executor.py # gallery-dl execution/config
│  ├─ browser_manager.py / browser_watcher.py
│  ├─ alt_paths.py    # alternate-path sync
│  ├─ app_control.py  # GUI tray / window & server lifecycle
│  ├─ updater.py      # GitHub Releases update check/apply
│  ├─ version.py      # app version
│  ├─ kde.py, sites.py, site_url.py, utils.py, error_logger.py
├─ frontend/          # static web UI (HTML/CSS/JS)
├─ assets/            # icons (icon.png, LAFhvst.ico)
├─ packaging/         # PyInstaller spec / icon generation
└─ build.ps1          # EXE build script
```

> The legacy HVST migration tool lives in a separate repo
> [mmoisall/LAF](https://github.com/mmoisall/LAF).

### License

[MIT](LICENSE) © 2026 mmoisall

---

<a id="korean"></a>
[English](#english) | **한국어**

## 한국어

FastAPI + pywebview 기반의 소셜 미디어 아카이빙 엔진. [gallery-dl](https://github.com/mikf/gallery-dl)을
백엔드로 사용해 여러 사이트의 미디어를 주기적으로 수집하고, 폴더/소스 트리, 프로필, 스케줄러,
감시(watcher), 로그, 대체경로 동기화를 하나의 로컬 웹 UI에서 관리한다.

### 주요 기능

- **소스/폴더 관리**: 사이트·키 기반 소스와 이를 묶는 폴더를 트리로 관리
- **스케줄러**: 주기 실행, 사이클, 재학습(relearn) 기반 업로드 주기 추정
- **프로필**: 사이트별 로그인 세션(Playwright persistent context) 관리 및 프로필 그룹
- **Watcher**: 사이트 세션을 주기적으로 새로고침하며 이벤트 수집
- **Reactive Rules**: 키워드 패턴 기반 자동 분류 규칙
- **대체경로(alt/cmb)**: 원본 폴더를 하드링크/복사로 동기화, 평탄 병합, prune
- **로그/오류 분석**: gallery-dl 오류 로그 수집·분류·해결 상태 관리
- **탐색기(Explorer)**: 서버 파일 탐색, 배치 이동/편집/삭제/실행, 썸네일
- **HVST 이관**: 구형 `hvst.db` → `lafhvst.db` 이관 도구는 별도 저장소 [mmoisall/LAF](https://github.com/mmoisall/LAF) 참고

### 요구 사항

- Windows (pywebview 기반 데스크톱 창), Python 3.11+
- 의존성: `requirements.txt` 참고 (FastAPI, uvicorn, pywebview, SQLAlchemy, APScheduler, gallery-dl, playwright, Pillow)

### 설치

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 실행

```bash
python main.py            # 데스크톱 창 + 로컬 서버 (기본 포트 17363)
python main.py --serve    # 창 없이 서버만 실행 (또는 LAF_NO_GUI=1)
```

실행하면 로컬(`http://127.0.0.1:17363`) 및 동일 네트워크 주소가 콘솔에 표시된다.

### EXE (Releases)

[Releases](https://github.com/mmoisall/LAFhvst/releases)에서 `LAFhvst-<버전>-win64.zip`을 받아
압축을 풀고 실행한다. 데이터(`lafhvst.db`, `data/`, `log/`)는 exe 옆에 생성되므로 폴더째로
이동/백업할 수 있다.

| 실행 파일 | 설명 |
| --- | --- |
| `LAFhvst.exe` | 데스크톱 창(GUI). 트레이 상주 지원 |
| `LAFhvst-server.exe` | 창 없는 서버(콘솔). 배너/로그 표시, `Ctrl+C` 종료 |
| `gallery-dl.exe` | 내장 gallery-dl (자동 사용) |

- 서버 포트: `--port 17363` 또는 `LAF_PORT` 환경변수
- 요구 환경: Windows 10/11, WebView2 런타임(기본 탑재)
- 프로필 로그인/감시 기능: 시스템 Chrome 또는 Edge 필요
- 픽시브 ugoira(webm) 변환: 시스템 ffmpeg 필요(없으면 해당 변환만 생략)

#### 트레이 · 서버 모드

- 창의 **X(닫기)** → 앱 종료 대신 **트레이로 숨김**, 서버는 계속 실행
- 홈 화면 우상단 **"서버 모드"** 버튼 → 동일하게 트레이로 숨김
- 트레이 아이콘 **우클릭** → `열기` / `서버 주소` / `종료`
- 완전 종료는 트레이 메뉴의 **종료**
- 설정 → "창 닫으면 트레이로 (서버 유지)" 로 동작 on/off (끄면 X 시 종료)

소스에서 직접 빌드:

```powershell
./build.ps1 -Version 0.1.2
# 산출물: dist/LAFhvst-0.1.2-win64.zip
```

### 업데이트

앱 시작 시 GitHub Releases에서 새 버전을 확인한다. 자세한 동작은 **설정 → 업데이트**:

- **시작 시 업데이트 확인** (기본 on)
- **자동 업데이트 (다운로드·설치)**: 새 버전이 있으면 자동 다운로드·설치 후 재시작 (패키징 exe 전용)
- **지금 확인** 버튼

새 버전이 있으면 홈 화면에 배너가 표시되고 **업데이트 설치**로 즉시 적용할 수 있다.
업데이트는 앱을 종료한 뒤 파일을 교체하고 재시작하며, 데이터(`data/`, `lafhvst.db`, `log/`)는
보존된다. 소스 실행에서는 확인만 되고 설치는 비활성화된다.

### 데이터 및 설정

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

### 구형 HVST 이관 (별도 저장소)

구형 HVST(=`LAFhvst old`)의 `hvst.db` 를 현재 `lafhvst.db` 로 이관하는 도구는
별도 저장소 **mmoisall/LAF** 에서 관리한다.

- 저장소: https://github.com/mmoisall/LAF (`hvst_import/`)
- 사용: `python import_hvst.py --lafhvst "<LAFhvst 경로>" --db "<hvst.db 경로>" --apply`

### 디렉터리 구조

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
│  ├─ app_control.py  # GUI 트레이/창·서버 생명주기
│  ├─ updater.py      # GitHub Releases 업데이트 확인/적용
│  ├─ version.py      # 앱 버전
│  ├─ kde.py, sites.py, site_url.py, utils.py, error_logger.py
├─ frontend/          # 정적 웹 UI (HTML/CSS/JS)
├─ assets/            # 아이콘 (icon.png, LAFhvst.ico)
├─ packaging/         # PyInstaller 스펙 / 아이콘 생성
└─ build.ps1          # EXE 빌드 스크립트
```

> 구형 HVST 이관 도구는 별도 저장소 [mmoisall/LAF](https://github.com/mmoisall/LAF) 에 있습니다.

### 라이선스

[MIT](LICENSE) © 2026 mmoisall
