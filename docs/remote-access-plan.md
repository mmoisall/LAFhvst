# LAFhvst 원격 접속(포트포워딩 없이 QR/페어링 링크로 제어) 개발 계획

작성 기준: 현재 워크트리 코드 (`server.py` 2,900+줄, `main.py`, `core/*`, `frontend/*`).

**확정된 결정 (§11)**
- 전송: **T3 자체 릴레이**(VPS + 도메인, `wss` 역방향 터널 + HTTP 멀티플렉싱) — 주소 영구 고정, 서드파티 의존 없음.
- 클라이언트: **C1 PWA 먼저** (기존 웹 UI 승격). 네이티브 래퍼는 실사용 후 선택.
- 파괴적 작업: **옵트인 토글**. 기본 원격 스코프는 control.

---

## 0. 구현 현황 (M1~M5 완료)

| 마일스톤 | 상태 | 산출물 |
| --- | --- | --- |
| M1 인증 코어 | 완료 | `core/remote_access.py`, `models.RemoteDevice`, `RemoteGuardMiddleware`, `/api/remote/*`, `/api/pair/claim` |
| M2 전용 리스너 | 완료 | `core/remote_listener.py` (`RemoteOnly`/`RemoteListener`) |
| M3 페어링 UI | 완료 | `frontend/pair.html`, `frontend/js/remote.js`, 설정 카드, `api.js` Bearer/401 |
| M4 릴레이 서버 | 완료 | `relay/` (protocol.md, relayd, mux, registry, Caddyfile, systemd, README, tests) |
| M5 릴레이 에이전트 | 완료 | `core/remote_tunnel.py`, lifespan 자동 시작/종료, 상태 API |
| M6 원격 정지 | 완료 | `gdl_executor.CancelToken`, `POST /api/sources/{id}/stop`, 홈 큐/DB 행 정지 버튼 |
| M6 SSE·알림 | 완료 | `GET /api/events`(SSE), `core/notifier.py`(웹훅), 설정 UI + 테스트 전송, 수집 완료/실패 알림 |
| M6 Web Push | 보류(사용자 결정) | 웹훅(ntfy 등)으로 대체하기로 함. 필요해지면 `pywebpush` + SW `push` 핸들러 + 구독 테이블로 재개(요구사항은 릴레이 https 배포·PWA 설치·iOS 16.4+) |
| M7 PWA 승격 | 완료 | `frontend/manifest.webmanifest`, `frontend/sw.js`, `frontend/icons/*` |
| M8 릴리스/런북 | 완료 | README 원격·정지·PWA·SSE·알림 섹션, `relay/README.md` 런북, PyInstaller 스펙 반영, `docs/release-notes.md` + 릴리스 워크플로 노트 구성, 버전 0.2.0 |
| M9 E2E 암호화 | 완료 | `core/remote_crypto.py`(ECDH P-256+HKDF+AES-256-GCM), `frontend/js/e2e.js`(WebCrypto), `/api/e2e`, `tests/vectors/e2e_v2.json` |
| M9 네이티브 래퍼(Capacitor) | 미착수(차단) | 이 환경에 Android SDK/gradle 이 없어 APK 빌드·검증이 불가 → 검증 불가능한 스캐폴드만 남기지 않기 위해 보류 |

**검증 결과(실측)**
- 앱: `pytest tests` 110 passed (`test_remote_access`, `test_remote_tunnel`, `test_remote_stop`, `test_events`, `test_notifier`, `test_notify_api`, `test_pwa`, `test_remote_crypto`, `test_e2e`).
- 릴레이: `cd relay && pytest tests` 37 passed.
- 폰(WebCrypto) 모듈: `node tests/e2e_vectors.test.js` 54 checks — 파이썬이 만든 벡터를 JS 가 복호화(언어 간 레이아웃 일치), 카운터 지속성/리셋, 음성 케이스.
- E2E(릴레이 경유, 실제 `frontend/js/e2e.js` + Node 폰): 24 checks — v2 페어링, 세션 봉투 왕복,
  **HTTP 레벨에 Authorization 헤더 없음**, 봉투에 토큰/경로 평문 없음, 스코프(403), 재전송 거부(401),
  새로고침 후 카운터 지속, v1 Bearer 하위 호환.
- **패키징 exe(0.2.0) 동일 검증 24 checks 통과** → `cryptography` 번들 확인.
- E2E(릴레이 경유, '폰' 역할 스크립트): 33 checks — 공개 페이지, 미인증 401, 페어링 등록, 인증 API 11종,
  `reveal`/프로파일 실행 403, 파괴적 작업 옵트인 전/후, 기기 회수 401, host_id 형식 404 / 오프라인 503, `disable` 후 503.
- E2E(SSE·알림): 13 checks — SSE 첫 프레임 0.02초 도착(버퍼링 없음), `dashboard` 페이로드, 웹훅 전달, 상태 조회.
- 데스크톱(loopback) 경로: 30 checks 통과(설정 저장/지속, QR, enable/disable, 잘못된 릴레이 오류 표시).
- 서비스 워커 로직: Node 스텁 하네스 14 checks 통과(오프라인 셸 폴백, 썸네일 캐시, **API JSON 미캐시**, 정적 캐시, 교차 origin 무개입).
- **미실시**: 브라우저 자동화 호스트가 없어 UI 시각 검증(QR 렌더링/레이아웃/설치 배너)은 수동 확인 필요.
  또한 서비스 워커는 보안 컨텍스트에서만 등록되므로 **https(릴레이) 접속에서만** 동작한다.
  Capacitor 네이티브 래퍼는 Android SDK 부재로 미착수(빌드·검증 불가).

**계획 대비 편차(구현 확정)**
1. **원격 리스너 기동 시점**: 앱 부팅 시가 아니라 *원격 접속을 켤 때(릴레이 사용 시)* 기동한다. LAN 전용 모드에서는 메인 리스너가 원격 요청을 그대로 처리(토큰 필수)하므로 두 번째 리스너가 불필요하다. 종료는 lifespan teardown에서 처리한다.
2. **설정 UI 위치**: 별도 모달 대신 설정 탭의 카드(`#remoteCard`). `remoteRelayUrl`/`remoteAllowDestructive`/`remoteAutoStart`는 기존 폼 저장 경로, `remoteEnabled`는 전용 엔드포인트(켜기/끄기 버튼)로 분리했다.
3. **페어링 토큰 수명 정책**: 활성 토큰 1개. 기기 등록 성공 시 자동 회전(QR 즉시 갱신)하고, 원격 접속이 켜져 있는 동안에는 항상 활성 상태를 유지한다(앱 재시작 후 자동 시작 시에도 QR 표시).
4. **`host` 헤더 재작성**: 릴레이는 에이전트의 로컬 포트를 모르므로 `127.0.0.1`로 재작성하고, 에이전트가 자기 대상(`127.0.0.1:<PORT+1>`)으로 다시 설정한다 → 앱이 보는 값은 항상 로컬 주소. `relay/protocol.md` §4를 이에 맞게 갱신했다.
5. **본문 처리**: 에이전트는 요청/응답 본문을 버퍼링한다(앱에 스트리밍 응답이 없음). 상한 요청 8 MiB / 응답 16 MiB, 초과 시 502.
6. **`_require_local_only`**: `reveal`, 프로파일 `launch`, `pixiv-token`을 IP 판정이 아니라 원격 리스너 플래그 기준으로 차단한다.
7. **파괴적 작업 게이트 이중화**: 미들웨어에서 경로 기준으로 먼저 차단하고(본문 검증 422보다 앞서도록), 각 엔드포인트에서도 `_require_scope`로 재확인한다.
8. **원격 정지 구현 방식**: 취소 신호는 `gdl_executor.CancelToken`(프로세스 핸들 보유)으로 전달한다. 서버는 소스별 토큰을 `_cancel_tokens`에 보관하고, 정지 시 `kill()` + 재시도 루프 탈출. 취소된 실행은 실패로 기록하지 않고 INFO 로그만 남긴다. 정지 스코프는 `control`(파괴적 아님)이라 `remoteAllowDestructive` 없이도 원격에서 허용된다.
9. **PWA**: 서비스 워커는 정적 셸과 썸네일 이미지만 캐시하고 **API JSON 은 절대 캐시하지 않는다**(인증·신선도). 등록은 보안 컨텍스트에서만 되므로 https(릴레이) 접속에서만 동작하고, LAN http 접속에서는 등록되지 않는다.
10. **에이전트 로컬 홉**: 응답 스트리밍을 위해 `http.client` 를 쓰지 않는다(청크 응답에서 요청 크기만큼 모일 때까지 블록되어 SSE 가 버퍼링된다). raw 소켓 + **HTTP/1.1 keep-alive 연결 풀**(요청당 새 연결 금지)로 통신하고 chunked/content-length/close 프레이밍을 모두 처리한다. 이 환경(Windows loopback)에서 요청당 새 연결을 열면 간헐적 RST(WinError 10054/10022)로 실패율이 높아, 연결 재사용이 정확성과 지연 모두에 유리하다. 연결 단계 실패는 1회 재시도한다(응답 수신 후에는 재시도하지 않음).
11. **SSE 클라이언트**: `EventSource` 는 커스텀 헤더를 못 쓰므로 `fetch` + `ReadableStream` 으로 직접 파싱해 `Authorization: Bearer` 를 유지한다(토큰을 URL 에 노출하지 않음). 연결 중에는 4초 폴링을 건너뛰고, 끊기면 폴링으로 폴백한다.
12. **알림**: 외부 푸시 서비스 없이 검증 가능한 **웹훅**(ntfy/Discord/Slack/Telegram/JSON/텍스트)으로 구현했다. Web Push 는 외부 서비스(FCM/Mozilla) 없이는 전달을 검증할 수 없어 보류(미검증 기능을 넣지 않는다는 원칙).
13. **E2E 암호화**: 릴레이 구간은 앱 계층 암호화(ECDH P-256 + HKDF-SHA256 + AES-256-GCM)로 보호한다. 봉투는 `POST /api/e2e` 하나로 보내고, 그 안의 요청은 **원격 리스너를 경유**해 실행한다(미들웨어·스코프 검증을 우회하지 않음). 릴레이는 경로·헤더·기기 토큰을 볼 수 없다. 봉투는 단일 왕복이라 SSE 와 함께 쓸 수 없어, E2E 활성 시 폰은 폴링으로 전환한다. `cryptography` 가 없으면 평문(v1) 페어링으로 폴백한다(앱은 계속 기동).
14. **재전송 방지**: 폰은 요청 카운터를 localStorage 에 기기별로 저장해 새로고침/탭 간에도 단조 증가를 유지하고, 앱은 기기 행에 마지막 카운터를 저장해 `counter <= last` 를 거부한다.
15. **연결 재사용 버그 발견·수정**: 에이전트가 keep-alive 연결을 재사용하면서 서버(uvicorn)의 유휴 타임아웃(기본 5초)에 먼저 닫힌 연결을 잡으면 간헐적으로 502 가 났다. → 원격 리스너의 `timeout_keep_alive` 를 75초로 올리고, 풀에 유휴 TTL(60초)을 두고, 요청이 **전달되기 전** 실패한 경우(또는 멱등 메서드)에만 새 연결로 1회 재시도한다. 회귀 테스트 3개(`test_local_pool_*`)로 고정했다.
16. **chunked 트레일러 미소비 버그**: `_iter_body` 가 chunked 종료 청크 뒤 `\r\n` 을 소비하지 않아 풀에 반납된 연결이 오염됐고, 다음 요청이 **본문은 정상인데 상태 502** 로 반환됐다(릴레이 로그의 `status=502 bytes=<정상 크기>` 가 증거). 트레일러까지 소비하도록 고치고, 상태줄 파싱 실패는 조용한 502 대신 오류로 처리한다. 회귀 테스트 3개(`test_iter_body_consumes_chunked_*`, `test_pool_reuse_after_chunked_response`)로 고정.
17. **lifespan 중복 실행 버그**: 원격 리스너가 같은 ASGI 앱을 다시 띄우면서 앱 lifespan 이 두 번 돌아 **에이전트가 2개** 붙었고, 나중 세션이 이전 세션을 대체하며 그 순간의 요청이 502/503 이 됐다. 원격 리스너는 `lifespan="off"` 로 띄우고, `_remote_start_transport` 는 이미 연결돼 있으면 아무것도 하지 않도록 멱등하게 만들었다(스케줄러 중복 기동도 함께 해소).
18. **연동 테스트 러너 제공**: `tests/integration/local_relay.py`(릴레이+앱+원격 리스너를 한 프로세스에서 띄우고 34개 항목 검증, `--keep` 으로 수동 테스트용 유지) + `tests/integration/phone_webcrypto.js`(브라우저 대신 Node 로 실제 `frontend/js/e2e.js` 실행). 런북은 `tests/integration/README.md`.

---

## 1. 목표 / 비목표

**목표**
- 외부 네트워크(4G/5G, 타 Wi-Fi)에서 휴대폰으로 자기 PC의 LAFhvst 서버를 제어한다.
- 공유기 포트포워딩, 고정 IP, DDNS 불필요 (PC → 릴레이 **아웃바운드** 연결만 사용).
- 최초 연결은 **QR 스캔 1회 또는 페어링 링크 1회**로 끝나고, 이후 주소가 고정이라 재페어링이 필요 없다.
- 노출 표면은 기존 웹 UI/API 재사용 (새 서버 로직 최소화).

**비목표 (v1)**
- 다중 사용자/조직 계정, 권한 그룹.
- 모바일에서 데스크톱 전용 기능: `reveal`(탐색기 열기), 프로파일 브라우저 실행(Playwright 로그인 창) → 원격에서는 **항상 403**.
- 원본 미디어 전송: 원격은 기존 400px 축소본 경로만 사용.
- 종단간(E2E) 암호화: v1은 릴레이가 TLS를 종단하는 **신뢰 모델**(자가 호스팅이므로 사용자가 릴레이를 통제). §9에 후속 옵션 기록.

---

## 2. 현재 코드 기준 제약 (사실 근거)

| 항목 | 현황 | 근거 |
| --- | --- | --- |
| 서버 | FastAPI + uvicorn 단일 프로세스, 정적 UI를 `/`에 mount | `server.py:2806`, `main.py:83-89` |
| 바인딩 | `0.0.0.0:17363` → **이미 LAN 원격 접속 가능** | `main.py:13-14`, `main.py:73-75`(LAN 주소 배너) |
| 인증 | **전무.** 미들웨어/`Depends`/CORS 설정 없음 | `server.py` 전체에 security import 없음 |
| 로컬 전용 가드 | IP 기준 2곳뿐 (`reveal`) | `server.py:1293-1298`, `:1304`, `:1320` |
| 원격 축소본 | 비-loopback 클라이언트에 400px 썸네일 서빙 | `server.py:1591-1594`, `:2790-2792`, `ensure_thumbnail`(`server.py:74-87`, `THUMBNAIL_MAX_PX=400`) |
| 실시간성 | **WebSocket/SSE 없음.** 전부 폴링 | `/api/dashboard/live` `:665`, `/api/watcher/events?since=` `:1962`, `/api/explorer/running` `:2523`, `/api/profiles/launching` `:1681` |
| 프론트 | `window.location.origin` 기준 상대 호출 → 리버스 프록시/터널에 투명 | `frontend/js/api.js:4` |
| 모바일 대응 | `viewport-fit=cover` + `@media (pointer: coarse)` 등 9개 브레이크포인트 | `frontend/index.html:5`, `frontend/css/style.css:95,687,1455,1888,...` |
| 작업 취소 | **불가.** 수집 프로세스가 `await` 인라인, 레지스트리 없음 | `core/gdl_executor.py:466-469`, `server.py:2290-2300`, `:2519-2521` |
| 설정 저장 | `DEFAULT_SETTINGS`(server.py) + `models.get_app_settings()`(DB) | `server.py:88-136`, `:355`, `core/models.py:394` |
| 마이그레이션 | `_MIGRATIONS`(ALTER) + `Base.metadata.create_all`(신규 테이블 자동 생성) | `core/database.py:14,138` |
| 패키징 | PyInstaller. `data/`·`lafhvst.db`·`log/`는 업데이트 시 보존 | `packaging/LAFhvst.spec`, README "Updates" |
| 테스트 | 테스트 인프라 없음 (pytest/httpx 미도입) | 저장소에 `tests/` 없음, `requirements.txt` |

**핵심 결론:** 인터넷 노출을 막는 것은 터널이 아니라 **인증**이다. 현재 API는 파일 삭제(`/api/explorer/batch-delete` `:2050`), 프로파일 리셋, 대량 수집을 인증 없이 허용한다. 따라서 **M1(인증) → 릴레이(M4/M5)** 순서는 선택이 아니라 필수다.

---

## 3. 아키텍처

```
[폰 PWA]                          [VPS: relay.<도메인>]                [PC (사내망/NAT 뒤)]
  https  ────  HTTPS  ────►  Caddy(TLS) → relayd :8788
                                    │  host_id 로 라우팅
                                    ▼
                            WS 멀티플렉서 ◄── wss 아웃바운드 ── RelayAgent (앱 내부)
                                                                    │ 127.0.0.1:17364
                                                                    ▼
                                                            RemoteOnly 래퍼
                                                            (비-loopback 강제)
                                                                    │
                                                            앱 서버 0.0.0.0:17363
```

**L1 전송(릴레이)** — PC가 먼저 나가므로 포트포워딩 불필요. 공개 주소는 `https://relay.<도메인>/h/<host_id>`로 **영구 고정**.
**L2 인증(기기 토큰)** — §4. 릴레이는 인증을 종단하지 않고 `Authorization`을 그대로 앱에 전달한다(앱이 유일한 권한 주체).
**L3 클라이언트(PWA)** — 기존 웹 UI를 반응형 PWA로 승격(§6).

대안(폴백, 필요 시 `Tunnel` 구현체 교체): T1 cloudflared Quick Tunnel은 **개발 스파이크 전용**, T2 Tailscale Funnel은 릴레이 운영이 불가능해질 때의 백업.

### 3.1 신뢰 경계 (중요)

현재 로컬/원격 구분은 `request.client.host`의 loopback 여부다 (`core/utils.py:112-118`은 문자열 기반 판정). 터널이 `127.0.0.1:17363`을 **직접** 가리키면 원격 사용자가 loopback으로 보여 썸네일 축소가 풀리고 `reveal` 가드가 무력화된다. 대응:

- 터널/릴레이는 **전용 리스너 `127.0.0.1:<PORT+1>`** 만 가리킨다 (§5.2).
- 전용 리스너의 래퍼가 **`scope["client"]`를 강제로 비-loopback으로 덮어쓴다**(신뢰 가능한 실 IP가 있으면 그 값, 없으면 placeholder `0.0.0.0`). 즉 신뢰 판정은 IP가 아니라 **리스너 플래그**가 결정한다 → uvicorn 버전·프록시 헤더 신뢰 정책에 의존하지 않는다.
- `X-Forwarded-For`는 클라이언트가 값을 앞에 붙일 수 있어(cloudflare/cloudflared#1426) **신뢰하지 않는다.** 실 IP는 릴레이가 검증해 넣는 전용 헤더(`X-LAF-Client-IP`)로만 받고, IP 리터럴 검증 후 사용하며 실패 시 placeholder로 폴백.
- 기존 `utils.is_loopback()` 기반 코드(축소본 `server.py:1591,2790`, `reveal` 403)는 **수정 없이 정확**해진다.
- 데스크톱 pywebview 창은 진짜 loopback이므로 토큰 없이 동작(비파괴). nonce 부트스트랩은 **불필요 → 채택 안 함**(설계 단순화).
- 실 IP는 로그/레이트리밋/감사 용도로만 쓰고, **접근 허용 판정에는 쓰지 않는다.**

---

## 4. 프로토콜 명세

### 4.1 페어링 링크 / QR 페이로드

```
lafhvst://pair?v=1&n=<서버 표시 이름>&u=https://relay.<도메인>/h/<host_id>&t=<1회용 토큰>&e=<만료 epoch>
```
QR에 실제로 인코딩되는 웹 폴백(앱 미설치 폰은 브라우저로 진입):
```
https://relay.<도메인>/h/<host_id>/pair.html#t=<1회용 토큰>&n=<서버 이름>
```
- 토큰을 **fragment**에 넣는다: 릴레이 액세스 로그·Referer·프록시 로그에 남지 않는다.
- TTL 5분, 1회용, 사용 즉시 폐기. 데스크톱 UI에 남은 시간 카운트다운.
- QR은 앱이 생성(순수 파이썬 `segno`): 데스크톱 창 PNG data URI, `--serve` 콘솔 모드 ANSI QR, 설정 모달에 페어링 링크 복사 버튼.

### 4.2 토큰 수명

| 토큰 | 발급 | 수명 | 저장 |
| --- | --- | --- | --- |
| 페어링 토큰 | 원격 접속 켤 때 / 수동 재발급 | 5분, 1회용 | 서버: SHA-256 해시만 |
| 기기 토큰 | `/api/pair/claim` 성공 시 | 무기한, 회수 가능 | 서버: SHA-256 해시 / 폰: `localStorage`(PWA) |
| 에이전트 토큰(`agent_token`) | PC 최초 활성화 시 생성 | 무기한 | PC DB + 릴레이 호스트 레지스트리(해시) |

발급은 `secrets.token_urlsafe(32)`, 비교는 `hmac.compare_digest`.

### 4.3 앱 신규 엔드포인트

| 메서드 | 경로 | 접근 | 설명 |
| --- | --- | --- | --- |
| GET | `/api/remote/status` | loopback+기기 | 활성 여부, 릴레이 상태, 공개 URL, 기기 목록, 마지막 오류 |
| POST | `/api/remote/enable` | loopback | RelayAgent 기동 + 페어링 토큰/QR 생성 |
| POST | `/api/remote/disable` | loopback | 연결 종료 → 공개 엔드포인트 즉시 소멸 |
| POST | `/api/remote/rotate` | loopback | 페어링 토큰 재발급(QR 갱신) |
| DELETE | `/api/remote/devices/{id}` | loopback | 기기 즉시 회수 |
| GET | `/api/remote/qr.png` | loopback | QR 이미지 |
| POST | `/api/pair/claim` | 공개(레이트리밋) | `{t, name}` → `{device_id, device_token, host_id, server}` |
| POST | `/api/sources/{id}/stop` | 기기(control) | 실행 중 수집 취소 (§5.4) |
| GET | `/api/events` (M6, 선택) | 기기 | SSE: dashboard/watcher 이벤트 단일 연결 |

`/api/pair/claim` 보호: IP당 분당 5회, 실패 10회 시 15분 차단, 실패 사유 비구분(토큰 오라클 방지).

### 4.4 권한 스코프

`view < control < manage`. 기본 원격 역할 = **control**.
- `manage`(batch-delete/move `:2050,:2034,:2014`, 프로파일 reset/delete `:1764,:1755`, 로그 clear `:2646`)는 설정 `remoteAllowDestructive`(기본 **false**)가 켜진 기기에만 부여.
- `reveal` `:1302,:1318`, 프로파일 `launch` `:1797`은 스코프와 무관하게 원격 항상 403.

---

## 5. 서버(PC) 변경 상세

### 5.1 인증 코어 — 신규 `core/remote_access.py`
- `issue_pair_token() / claim_pair_token(t) / issue_device_token(name) / verify_device(token) -> DeviceCtx|None / revoke(device_id)`
- `DeviceCtx(role, name, remote: bool)` → 미들웨어가 `request.state`에 주입.
- QR/링크 빌더(`build_pair_link`, `render_qr`) + `agent_token`/`host_id` 생성·보관.

**models.py**: `RemoteDevice` 테이블 추가 → `create_all`(`core/database.py:138`)이 자동 생성, 마이그레이션 불필요.
필드: `id, name, token_hash(unique), role, created_at, last_seen_at, last_ip, user_agent, revoked_at`.

**server.py**
- `RemoteGuardMiddleware`(`@app.middleware("http")`): loopback이고 `remote_listener` 아님 → 통과. 그 외는 경로 허용목록(`/`, 정적, `/pair.html`, `/api/pair/claim`, `/api/version`) 또는 유효한 Bearer 기기 토큰일 때만 통과. 실패 `401` + `WWW-Authenticate`. 성공 시 `last_seen_at` 갱신(분당 1회 쓰기 제한).
- `remoteEnabled=true`인데 유효 기기가 0개면 원격 요청 전체 `503`(자기잠금 방지 위해 loopback 예외).
- 파괴적 엔드포인트에 `require_scope("manage")`.
- `DEFAULT_SETTINGS`(`server.py:88`) 추가: `remoteEnabled:False`, `remoteAllowDestructive:False`, `remoteRelayUrl:""`, `remoteHostId:""`, `remoteThumbnailMaxPx:400`, `remoteAutoStart:False`.

### 5.2 전용 리스너 — `main.py`
- 원격이 켜지면 두 번째 `uvicorn.Server`를 `127.0.0.1:PORT+1`에 기동(`proxy_headers` 미사용, `loop="server:proactor_loop_factory"` 재사용).
- 신뢰 경계 래퍼:
  ```python
  class RemoteOnly:
      """터널/릴레이 전용 리스너. 모든 요청을 '원격'으로 표시하고 실 IP를 복원한다."""
      def __init__(self, app, ip_header=b"x-laf-client-ip"): ...
      async def __call__(self, scope, receive, send):
          if scope["type"] == "http":
              raw = dict(scope["headers"]).get(self.ip_header)
              ip = _valid_ip_literal(raw)                 # 실패 시 None
              scope["client"] = (ip or "0.0.0.0", 0)      # 절대 loopback이 되지 않게
              scope.setdefault("state", {})["remote_listener"] = True
          await self.app(scope, receive, send)
  ```
- 미들웨어는 `state["remote_listener"]`를 **유일한 신뢰 경계**로 삼는다. 릴레이 경유 요청은 IP와 무관하게 항상 원격 + 토큰 필수.
- 기존 메인 리스너(`0.0.0.0:17363`)는 그대로 → LAN 접속자도 loopback이 아니므로 원격 취급되어 토큰 필요.
- 리스너는 `127.0.0.1` 전용이므로 Windows 방화벽 인바운드 규칙 추가 불필요.

### 5.3 RelayAgent — 신규 `core/remote_tunnel.py`
- `class Tunnel(Protocol)`: `start()/stop()/state()->{running,url,error,uptime,queued}` → 릴레이/대안 교체 지점.
- `RelayAgent` 구현: `wss://relay.<도메인>/agent`로 아웃바운드 연결, `hello{host_id, agent_token, version, label}` 전송, 20초 ping, 재연결 백오프 1→2→4→…30초(지터), 릴레이가 준 `404/409`(host_id 선점)는 재시도 금지하고 UI에 표면화.
- 요청 처리: 릴레이 프레임을 받아 **`http://127.0.0.1:PORT+1`로 로컬 HTTP 요청**을 보내고 응답을 프레임으로 되돌린다(aiohttp/httpx 스트리밍). ASGI 인프로세스 배선 대신 루프백 HTTP 1홉을 쓰는 이유: 신뢰 경계가 한 곳(§5.2)으로 고정되고, uvicorn의 검증된 HTTP 처리를 재사용해 6개월 유지비가 낮다.
- 스트림 제한: 동시 스트림 16, 스트림당 버퍼 4 MiB, 프레임 1 MiB, 15초 무진행 스트림 취소, 요청 타임아웃 120초.
- 수명주기: `core/app_control.py`의 `quit()`에서 `tunnel.stop()` — 트레이 상주 중에는 연결 유지가 기본. 앱 시작 시 `remoteAutoStart`면 자동 기동.
- 부가: 연결/트래픽/오류 카운터를 `/api/remote/status`로 노출.

### 5.4 원격 취소(정지) — 제어의 핵심
현재 수집은 취소 불가(§2). 변경:
- `server.py`에 `_active_procs: dict[int, asyncio.subprocess.Process]` + `_cancel_requested: set[int]` 추가.
- `_collect_source`(`server.py:2290`)에서 프로세스 등록/해제, `_cancel_requested` 확인 시 `proc.kill()`.
- `POST /api/sources/{id}/stop`: 플래그 설정 + `kill()` → 상태 `cancelled`(실패 로그 미기록).
- 프론트: 실행 카드에 정지 버튼(`/api/explorer/running` 폴링 연동).

### 5.5 프론트엔드
- `frontend/js/api.js`: `localStorage.lafDeviceToken`을 `Authorization: Bearer`로 부착. `401` → `/pair.html` 이동.
- `frontend/pair.html` (신규): fragment 토큰 수신 → `/api/pair/claim` → 토큰 저장 → `/` 리다이렉트. "브라우저로 계속 / 홈 화면에 추가" 안내.
- `frontend/js/modals.js` 설정 모달에 **"원격 접속"** 섹션: 켜기/끄기, 릴레이 주소, QR + 링크 복사, TTL 카운트다운, 기기 목록/회수, `remoteAllowDestructive` 토글, 연결 상태/오류 표시.
- `frontend/js/nav.js:5-10` 기준으로 원격 모드 탭 제한: `home, db(읽기), log, settings(원격 섹션)`.

### 5.6 릴레이 서버 — 신규 `relay/` (동일 저장소)

프로토콜과 구현이 한 곳에 있어야 버전 드리프트가 없다 → 이 저장소 `relay/`에 배치(별도 저장소 분리는 폭발 반경 축소가 필요해질 때).

| 파일 | 역할 |
| --- | --- |
| `relay/protocol.md` | §4 프레임 명세 단일 출처(`v1`) |
| `relay/relayd.py` | FastAPI + uvicorn: `/agent`(WS), `/h/{host_id}/...`(공개 HTTP), `/healthz` |
| `relay/registry.py` | host_id → agent_token_hash 최초 바인딩(선점 방지), 기기 수/메트릭(메모리) |
| `relay/mux.py` | 스트림 멀티플렉서: 요청 프레임 → 대기 future, 헤더/바디 스트리밍, 백프레셔, 취소 |
| `relay/deploy/Caddyfile`, `relay/deploy/lafhvst-relay.service` | TLS(Let's Encrypt 자동), WS 업그레이드, systemd |
| `relay/tests/` | 프로토콜 단위 + 실제 앱 대신 echo 백엔드로 E2E |

- **인증 분리 원칙**: 릴레이는 `Authorization` 헤더를 해석·검증하지 않고 그대로 전달한다. 앱만이 권한 주체다. 릴레이 차원의 방어는 (a) `host_id` 110bit 랜덤(추측 불가), (b) WS `hello`의 `agent_token` 검증, (c) 호스트당 레이트리밋, (d) 요청 바디 8 MiB 상한뿐.
- **주소 형태**: `https://relay.<도메인>/h/<host_id>/` → 내부적으로 경로 프리픽스를 벗겨 앱에 전달. `api.js`가 origin 기준이므로(`frontend/js/api.js:4`) 프리픽스만 맞으면 수정 불필요.
- **프라이버시**: 릴레이는 요청 본문/파일을 저장하지 않고, 로그는 메타데이터(시각/host_id/상태/바이트)만 남기고 30일 보존.
- **비용/대역폭**: 원격은 400px 썸네일(≈30KB)만 흐른다. 페이지 20장 기준 ≈0.6MB. PWA 서비스워커가 썸네일을 캐시해 반복 egress를 줄인다(VPS 트래픽 요금 대응).
- **운영**: `/healthz`, agent 연결 수/활성 스트림/오류율 메트릭(로그 기반으로 시작), 재시작 시 에이전트가 자동 재연결(재연결 백오프가 릴레이 배포를 흡수).

### 5.7 릴레이 프레임 명세 (v1, 요약)

WS 바이너리 프레임 = 1바이트 타입 + 4바이트 스트림 ID + 페이로드(JSON 또는 raw).

| 방향 | 타입 | 페이로드 |
| --- | --- | --- |
| agent→relay | `HELLO` | `{host_id, agent_token, version, label}` |
| relay→agent | `HELLO_OK` / `HELLO_ERR` | `{session}` / `{code:404|409|429, message}` |
| relay→agent | `REQ_HEAD` | `{id, method, path, headers, has_body}` |
| relay→agent | `REQ_BODY` / `REQ_END` | raw chunk / — |
| agent→relay | `RES_HEAD` | `{id, status, headers}` |
| agent→relay | `RES_BODY` / `RES_END` | raw chunk / — |
| 양방향 | `CANCEL` | `{id}` (클라이언트 이탈·타임아웃) |
| 양방향 | `PING`/`PONG` | 20초 |

동시성/백프레셔: 스트림별 링버퍼 상한 초과 시 `CANCEL`로 끊고 502를 반환한다(전체 세션을 죽이지 않는다). 프로토콜 버전은 `HELLO.version`으로 협상, 불일치 시 명시적 거부.

---

## 6. 클라이언트 앱 전략 (C1 PWA 확정)

| 항목 | 내용 |
| --- | --- |
| 매니페스트 | `frontend/manifest.webmanifest` (이름/아이콘/`display:standalone`/`start_url:./?remote=1`) |
| 서비스워커 | `frontend/sw.js`: 앱 셸 캐시(오프라인 셸), 썸네일 캐시(용량 상한 + LRU), `push`/`notificationclick` 훅은 M6 선택 |
| 반응형 | 기존 9개 브레이크포인트 재사용, 원격 모드에서 탭 제한(§5.5), 홈 위젯 세로 스택 |
| QR 스캔 | 링크 진입 방식이 기본(iOS 카메라/Android 렌즈). Android Chrome은 `BarcodeDetector`로 인앱 스캔 버튼 제공(미지원 브라우저는 숨김) |
| 저장 | 기기 토큰 `localStorage`, 회수 시 즉시 무효 |
| C2(보류) | Capacitor 래퍼: 네이티브 QR, Web Push, Keystore, 스토어 배포 — 실사용 후 필요 시 |

알림(M6 선택): ① 앱 열림 시 폴링 → ② Web Push(VAPID + `pywebpush`, SW) → ③ 외부 훅(ntfy/Telegram, 서버 인프라 0). 목적은 "수집 완료/오류" 통지.

---

## 7. 마일스톤

| ID | 내용 | 산출물 | 인수 기준 | 예상 |
| --- | --- | --- | --- | --- |
| **M0** | 위협 모델·정책 확정 | 결정 기록(§11) | 결정 3건 확정 | 0.5d |
| **M1** | 인증 코어 + 미들웨어 + DB | `core/remote_access.py`, `RemoteDevice`, `RemoteGuardMiddleware`, `/api/pair/claim` | LAN(`192.168.x.x`) 무토큰 API → 401; 페어링 후 200; revoke 즉시 401 | 2–3d |
| **M2** | 전용 리스너 + 신뢰 경계 | `main.py` 2번째 uvicorn, `RemoteOnly` | 릴레이 경유 요청에서 축소본 서빙·`reveal` 403 유지(테스트로 증명) | 1d |
| **M3** | 페어링 UI + QR | 설정 모달 원격 섹션, `pair.html`, `segno` | QR 스캔 → 기기 등록 → 폰 홈 대시보드 표시 | 1–2d |
| **M4** | **릴레이 서버** | `relay/` (protocol.md, relayd, mux, registry, Caddy+systemd), VPS 배포 | 로컬 에코 백엔드로 E2E 통과; `wss://relay.<도메인>/healthz` 200; agent 재연결 시 세션 복구 | 3–5d |
| **M5** | **RelayAgent(PC)** | `core/remote_tunnel.py`, 수명주기 훅, 상태 API | 사내망 PC에서 4G 폰으로 공개 URL 접속·API 왕복 성공; 앱 종료 시 엔드포인트 소멸 | 2–3d |
| **M6** | 원격 제어 보강 | `/api/sources/{id}/stop`, 스코프 가드, (선택) `/api/events` SSE, (선택) 알림 | 폰에서 실행 중 수집 정지 → `cancelled`; manage 없이 batch-delete 403 | 2–3d |
| **M7** | PWA 승격 | manifest, sw.js, 반응형 네비/홈 | 홈 화면 추가 후 재접속 자동, 오프라인 셸·썸네일 캐시 동작 | 2–3d |
| **M8** | 운영·문서·릴리스 | README 원격 섹션, 배포 런북(도메인/TLS/방화벽), 릴리스 노트 | 신규 설치 → 15분 내 원격 연결 성공(런북만 보고) | 1–2d |
| **M9** | (선택) E2E 암호화 / C2 래퍼 / 다중 기기 정책 | — | — | 후속 |

**선행 스파이크(0.5–1d, M4 착수 전):** VPS 없이 `cloudflared` Quick Tunnel로 폰 접속·터널/방화벽 변수를 먼저 제거한다. 코드로 남기지 않으며, M1/M2/M3 검증에도 재사용한다.

**임계 경로:** M1 → M2 → M3 → (M4·M5) → M6 → M7 → M8. M4(릴레이)와 M6(앱 기능)는 병행 가능 — 접점은 §5.7 프레임 명세뿐이며 M4에서 먼저 동결한다.

---

## 8. 보안 요구사항 체크리스트

- [ ] 무인증 인터넷 노출 금지: `remoteEnabled=true` + 유효 기기 0개 → 원격 API 전체 503(loopback 예외).
- [ ] 페어링 토큰 5분 TTL·1회용·해시 저장·실패 사유 비구분·IP 레이트리밋.
- [ ] 기기 토큰 `compare_digest` 비교, 회수 즉시 401.
- [ ] 로그인/페어링 토큰이 URL 쿼리가 아니라 **fragment**로만 이동, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `/api/*` `Cache-Control: no-store`.
- [ ] 원격 기본 스코프 control, `manage`는 명시적 옵트인(기본 off).
- [ ] 신뢰 판정은 `remote_listener` 플래그만. 클라이언트 제공 헤더(`X-Forwarded-For` 등)로 권한·로컬 판정 금지.
- [ ] 릴레이는 인증을 종단하지 않고 `Authorization`을 그대로 전달(단일 권한 주체 = 앱).
- [ ] `host_id`는 110bit 랜덤, 최초 바인딩 선점 방지, 릴레이 `agent_token` 검증.
- [ ] 파괴적 엔드포인트 원격 차단/옵트인, 모든 원격 변경 작업 감사 로그(`error_logger.log`, `scope="remote"`).
- [ ] 원격 사용 불가 확인: `remote/disable` 후 공개 URL 접속 실패, 기존 기기 토큰도 401.
- [ ] 기존 무인증 LAN 노출(0.0.0.0)도 M1에서 함께 해소 — 릴리스 노트에 명시(LAN 브라우저도 페어링 필요).

---

## 9. 리스크 & 대응

| 리스크 | 영향 | 대응 |
| --- | --- | --- |
| 터널/릴레이 → loopback 프록시로 원격이 "로컬"로 오인 | 썸네일 원본 노출, `reveal` 우회 | §3.1·§5.2의 전용 리스너 + `remote_listener` 플래그, 테스트로 고정 |
| 릴레이 SPOF/장애 | 원격 접속 불가 | 에이전트 자동 재연결(백오프), `/healthz` + 외부 모니터, T2(Tailscale Funnel) 폴백 구현체 유지 |
| VPS egress 비용 폭증 | 운영비 | 축소본만 전송(400px), PWA 썸네일 캐시, 호스트당 레이트리밋·일일 전송량 상한, 메트릭 경보 |
| 릴레이가 트래픽을 평문 열람 | 프라이버시 | 자가 호스팅(사용자 통제), 본문 미저장·메타데이터만 30일, v2에서 E2E(릴레이 TCP-over-WS + 앱 TLS) 검토 |
| `host_id` 선점/하이재킹 | 계정 탈취 유사 | 110bit 랜덤, 최초 바인딩 후 다른 토큰 거부, 선점 시도 로그 |
| WS 멀티플렉싱 버그(교착·누수·유실) | 요청 멈춤/메모리 증가 | 스트림별 링버퍼 상한 + `CANCEL`, 프레임 크기 상한, 프로토콜 단위 테스트 + 부하 테스트(동시 16 스트림) |
| 취소 기능으로 프로세스/세마포어 상태 꼬임 | 수집 멈춤/중복 실행 | `_active_procs`·`_cancel_requested`를 `finally`에서 정리, 취소 시 실패 로그 미기록 테스트 |
| 릴레이 배포 후 구버전 앱 호환 | 연결 실패 | `HELLO.version` 협상 + 명시적 거부 + 앱 UI에 "업데이트 필요" 표시 |
| 릴레이 도메인/TLS 만료 | 전면 장애 | Caddy 자동 갱신, 만료 30/7/1일 알림 |
| 인증 도입으로 기존 자동화(북마클릿/스크립트) 깨짐 | 사용자 불편 | 토큰 발급 후 `Authorization`만 추가, 릴리스 노트 마이그레이션 안내 |

---

## 10. 테스트/검증 계획

테스트 인프라가 없으므로 최소 도입: `pytest`, `httpx`(dev 의존, `requirements-dev.txt`).

1. **앱 (`tests/test_remote_access.py`, `fastapi.testclient`)**
   - `test_loopback_bypasses_token`, `test_lan_without_token_401`, `test_pair_claim_once_only`, `test_expired_pair_token_rejected`, `test_revoked_device_401`, `test_destructive_requires_manage_without_optin`, `test_remote_only_wrapper_forces_remote`(`scope["client"]`가 비-loopback이고 `utils.is_loopback()`이 False인지).
2. **릴레이 (`relay/tests/`)**
   - 프레임 인코딩/디코딩 왕복, 스트림 ID 재사용 금지, 상한 초과 시 `CANCEL`+502, WS 재연결 후 세션 회복, 에코 백엔드로 경로 프리픽스 제거 검증, 바디 8 MiB 초과 거부.
3. **E2E 스모크(필수, 수동)**
   - 사내망 PC + 4G 폰: QR 스캔 → 홈 로드 → 수집 시작 → 정지 → 로그 확인 → 기기 회수 후 401.
   - 릴레이 `relayd` 재시작 → 에이전트 자동 재연결 확인. 앱 종료 → 공개 URL 502/503(엔드포인트 소멸).
4. **회귀**: 데스크톱 창(loopback)은 토큰 없이 기존 기능 전부 동작(특히 `reveal`, 원본 썸네일).

---

## 11. 결정 기록

| 항목 | 결정 | 근거 |
| --- | --- | --- |
| Q1 전송 | **T3 자체 릴레이(VPS+도메인)** | 주소 영구 고정 → "1회 페어링 후 자동 재접속" 성립, 서드파티 정책/가용성 비의존. 비용은 VPS+도메인, 구현량 M4/M5 |
| Q2 클라이언트 | **C1 PWA 먼저** | 기존 웹 UI 100% 재사용, 심사·서명 파이프라인 불필요, iOS/Android 동일 |
| Q3 파괴적 작업 | **옵트인 토글** | 기본 차단 + 필요 시 `remoteAllowDestructive`로 manage 스코프 부여 |

**후속 결정(착수 시점):** 릴레이 도메인/서버 스펙(§5.6), M9의 E2E 암호화 및 C2 래퍼 진행 여부.
