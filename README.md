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
- **Remote control (QR pairing)**: control the server from a phone through a self-hosted relay
  (`relay/`) — no port forwarding; device tokens, scoped permissions, revocable devices
- **HVST migration**: the old `hvst.db` → `lafhvst.db` tool lives in a separate repo
  [mmoisall/LAF](https://github.com/mmoisall/LAF)

### Requirements

- Windows (pywebview desktop window), Python 3.11+
- Dependencies: see `requirements.txt` (FastAPI, uvicorn, pywebview, SQLAlchemy, APScheduler,
  gallery-dl, playwright, Pillow, segno, websockets)

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

### Remote access (QR pairing)

Control the running server from a phone without port forwarding. The PC opens an **outbound**
WebSocket to your own relay (`relay/`), so the phone reaches the app at a stable public URL.

1. Deploy the relay: see [`relay/README.md`](relay/README.md) (VPS + domain + Caddy TLS).
2. Settings → **원격 접속 (QR 페어링)**: enter the relay address, press **원격 접속 켜기**.
3. Scan the QR (or send the pairing link) on the phone. `/pair.html` claims a one-time token and
   stores the device token in `localStorage`.

Behaviour:

- Every non-loopback request (LAN **and** relay) requires a paired device token. The desktop window
  (loopback) is unaffected.
- Destructive actions (delete / move / reset / log clear) are refused for remote devices unless
  **원격 파괴적 작업 허용** is enabled.
- `reveal` (open Explorer) and profile login launch are desktop-only.
- Remote clients receive the 400px thumbnail path only (`THUMBNAIL_MAX_PX`).
- Pairing tokens are single-use, expire after 5 minutes, and rotate after each successful pairing;
  the device list and revoke buttons live in the same settings card.
- `원격 접속 끄기` stops the relay connection immediately (the public URL becomes unavailable).
- **Stop a running collection** from the phone: the home *Live queue* widget and the DB source row show
  a `■` button while a source is running (`POST /api/sources/{id}/stop`). Cancelling does not count as
  a failure and leaves only an INFO log.
- **Install as an app (PWA)**: over the relay (https) the browser offers "Add to Home screen" —
  manifest, icons and a service worker are served by the app itself. The service worker caches the
  shell and 400px thumbnails only; API JSON is never cached. It does not register over plain-http LAN.
- **Live updates (SSE)**: `GET /api/events` pushes dashboard changes and watcher events over one
  connection; the home screen skips its 4s polling while the stream is connected and falls back to
  polling when it drops.
- **Notifications (webhook)**: Settings → **알림 (웹훅)** — set a URL (ntfy / Discord / Slack /
  Telegram or a plain JSON/text endpoint), choose the body format, and toggle success/failure
  alerts. **테스트 전송** sends one immediately. Duplicate alerts are throttled per
  source+kind (`notifyMinIntervalSec`); cancelled runs never notify.
- **End-to-end encryption (E2E)**: the relay (and anything terminating TLS in front of it) only
  forwards opaque envelopes — request paths, headers and the device token live inside an
  AES-256-GCM payload. Keys are negotiated per device (ECDH P-256 + HKDF-SHA256) during pairing, and
  the app's public key travels in the QR/pairing link. Replayed envelopes are rejected.
  While E2E is active the phone polls instead of using SSE (an envelope is a single round trip).
  Requires `cryptography` on the PC; if it is missing the app still runs with plaintext (v1) pairing.
- Protocol contract: [`relay/protocol.md`](relay/protocol.md) (v1 frames + §9 E2E v2). Design notes:
  [`docs/remote-access-plan.md`](docs/remote-access-plan.md).
- Integration testing (relay + agent + phone end-to-end): [`tests/integration/README.md`](tests/integration/README.md)
  — `python tests/integration/local_relay.py` and `node tests/integration/phone_webcrypto.js`.

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

Release notes are composed by the release workflow from [`docs/release-notes.md`](docs/release-notes.md)
(curated highlights + breaking-change/migration notes) followed by the commit list since the previous
tag. **Update `docs/release-notes.md` before pushing a `v*` tag** — auto-generated notes alone miss
migration steps. Tag format: `v0.2.0` (the workflow passes the version to `build.ps1`).

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

### 원격 접속 (QR 페어링)

포트포워딩 없이 휴대폰에서 실행 중인 서버를 제어한다. PC가 **아웃바운드** WebSocket으로 자체
릴레이(`relay/`)에 연결하므로, 폰은 고정된 공개 주소로 앱에 도달한다.

1. 릴레이 배포: [`relay/README.md`](relay/README.md) 참고 (VPS + 도메인 + Caddy TLS).
2. 설정 → **원격 접속 (QR 페어링)**: 릴레이 주소를 넣고 **원격 접속 켜기**.
3. 폰으로 QR 스캔(또는 페어링 링크 전달) → `/pair.html` 이 1회용 토큰을 소비하고 기기 토큰을
   `localStorage` 에 저장한다.

동작 규칙:

- loopback 이 아닌 모든 요청(같은 네트워크 **및** 릴레이)은 페어링된 기기 토큰이 필요하다.
  데스크톱 창(loopback)은 영향받지 않는다.
- 파괴적 작업(삭제/이동/초기화/로그 비우기)은 **원격 파괴적 작업 허용**을 켜지 않으면 원격
  기기에서 거부된다.
- `reveal`(탐색기 열기), 프로파일 로그인 실행은 데스크톱 전용이다.
- 원격 클라이언트에는 400px 축소본 경로만 제공된다 (`THUMBNAIL_MAX_PX`).
- 페어링 토큰은 1회용·5분 만료이며 기기 등록 성공 시 자동 회전한다. 기기 목록과 회수 버튼은
  같은 설정 카드에 있다.
- **원격 접속 끄기**는 릴레이 연결을 즉시 종료한다(공개 주소 사용 불가).
- **실행 중 수집 정지**: 폰에서 홈 *작업 큐* 위젯과 DB 소스 행에 `■` 버튼이 표시된다
  (`POST /api/sources/{id}/stop`). 취소는 실패로 기록되지 않고 INFO 로그만 남는다.
- **앱처럼 설치(PWA)**: 릴레이(https)로 접속하면 브라우저가 "홈 화면에 추가"를 제공한다.
  매니페스트·아이콘·서비스 워커를 앱이 직접 서빙한다. 서비스 워커는 셸과 400px 썸네일만
  캐시하고 API JSON 은 캐시하지 않는다. 평문 http(LAN) 접속에서는 등록되지 않는다.
- **실시간 갱신(SSE)**: `GET /api/events` 가 대시보드 변화와 감시 이벤트를 한 연결로 밀어준다.
  연결 중에는 홈 화면의 4초 폴링을 건너뛰고, 끊기면 폴링으로 자동 복귀한다.
- **알림(웹훅)**: 설정 → **알림 (웹훅)** 에서 URL(ntfy / Discord / Slack / Telegram 또는 일반
  JSON·텍스트 엔드포인트)과 본문 형식, 완료/실패 알림 여부를 지정한다. **테스트 전송**으로 즉시
  확인할 수 있다. 같은 소스·같은 종류의 알림은 `notifyMinIntervalSec` 로 억제하고, 취소된
  실행은 알림을 보내지 않는다.
- **종단간 암호화(E2E)**: 릴레이(와 그 앞단의 TLS 종단)는 **불투명한 봉투**만 중계한다 — 요청 경로·
  헤더·기기 토큰이 모두 AES-256-GCM 암호문 안에 있다. 키는 페어링 시 기기별로 합의하며
  (ECDH P-256 + HKDF-SHA256), 앱 공개키는 QR/페어링 링크로 전달된다. 재전송된 봉투는 거부된다.
  E2E 사용 중에는 폰이 SSE 대신 폴링한다(봉투는 단일 왕복이므로). PC 에 `cryptography` 가
  필요하며, 없으면 평문(v1) 페어링으로 동작한다.
- 프로토콜 계약: [`relay/protocol.md`](relay/protocol.md) (v1 프레임 + §9 E2E v2) · 설계 기록:
  [`docs/remote-access-plan.md`](docs/remote-access-plan.md).
- 연동 테스트(릴레이+에이전트+폰 전체 경로): [`tests/integration/README.md`](tests/integration/README.md)
  — `python tests/integration/local_relay.py`, `node tests/integration/phone_webcrypto.js`.

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

릴리스 노트는 릴리스 워크플로가 [`docs/release-notes.md`](docs/release-notes.md)(사람이 관리하는
하이라이트 + 호환성 변화/마이그레이션 안내)와 직전 태그 이후 커밋 목록을 합쳐 만든다.
**`v*` 태그를 푸시하기 전에 `docs/release-notes.md` 를 갱신**해야 한다 — 자동 생성 노트만으로는
마이그레이션 단계가 누락된다. 태그 형식: `v0.2.0`(워크플로가 버전을 `build.ps1` 에 전달).

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
