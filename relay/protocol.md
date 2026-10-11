# LAFhvst 릴레이 프로토콜 v1

포트포워딩 없이 원격 제어하기 위한 자체 릴레이의 단일 계약(single source of truth).
구현: `relay/relayd.py`(서버), `core/remote_tunnel.py`의 `RelayAgent`(PC 측).

## 1. 구성

```
[폰 PWA] --HTTPS--> Caddy(TLS) --> relayd --WS(멀티플렉싱)--> RelayAgent(PC) --HTTP--> 127.0.0.1:PORT+1
```

- PC 는 **아웃바운드** `wss://relay.<도메인>/agent` 만 연다. 인바운드 포트/포트포워딩 불필요.
- 공개 주소: `https://relay.<도메인>/h/<host_id>/<앱 경로>` → 앱 경로 그대로 전달.
- 릴레이는 **인증을 종단하지 않는다.** `Authorization` 헤더를 그대로 전달하고, 권한 판정은 앱이 한다.
- 신뢰 경계는 앱의 `127.0.0.1:<PORT+1>` 원격 리스너(`core/remote_listener.py`)에 있다.

## 2. 프레이밍

WS **바이너리** 메시지 1개 = 프레임 1개.

```
byte 0      : type
byte 1..4   : stream_id (big-endian uint32, 0 = 세션 제어)
byte 5..    : payload (JSON(UTF-8) 또는 raw bytes)
```

| type | 이름 | 방향 | payload |
| --- | --- | --- | --- |
| 0x01 | HELLO | agent→relay | JSON `{host_id, agent_token, version, label}` (stream 0) |
| 0x02 | HELLO_OK | relay→agent | JSON `{host_id, session, streams_max}` |
| 0x03 | HELLO_ERR | relay→agent | JSON `{code, message}` → 전송 후 relay 가 소켓을 닫는다 |
| 0x10 | REQ_HEAD | relay→agent | JSON `{method, path, query, headers, has_body}` |
| 0x11 | REQ_BODY | relay→agent | raw chunk |
| 0x12 | REQ_END | relay→agent | (빈) 요청 본문 끝 |
| 0x20 | RES_HEAD | agent→relay | JSON `{status, headers}` |
| 0x21 | RES_BODY | agent→relay | raw chunk |
| 0x22 | RES_END | agent→relay | (빈) 응답 끝 |
| 0x30 | CANCEL | 양방향 | JSON `{reason}` |
| 0x40 | PING | 양방향 | (빈) |
| 0x41 | PONG | 양방향 | (빈) |

- JSON payload 는 UTF-8, 1 MiB 이하. 초과 프레임은 프로토콜 오류로 세션을 닫는다.
- `stream_id` 는 세션 내에서 단조 증가하며 재사용하지 않는다(0 은 세션 제어 전용).

## 3. 세션

1. agent 접속 → HELLO. `version` 은 `"1"`(불일치 시 relay 는 `HELLO_ERR{code:"version"}`).
2. relay 는 `host_id` 를 레지스트리에서 조회한다.
   - 미등록 → `sha256(agent_token)` 을 **최초 바인딩**하고 등록한다(영속 저장).
   - 등록됨 → 상수시간 비교로 일치해야 한다. 불일치 → `HELLO_ERR{code:"host_id_taken"}`.
   - `host_id` 는 앱이 생성한 110bit 이상 랜덤 문자열(`[A-Za-z0-9_-]{16,}`)이어야 하며 형식 위반은 `code:"bad_host_id"`.
3. 같은 `host_id` 로 새 세션이 인증되면 기존 세션은 닫는다(WS close code 4000, 사유 `replaced`). 앱 재시작 후 재연결을 흡수하기 위함이다.
4. HELLO_OK 이후 PING/PONG. relay 는 20초마다 PING, 60초 무응답이면 세션을 닫는다.

## 4. 요청/응답

- 클라이언트 요청 → relay 가 `stream_id` 를 할당하고 `REQ_HEAD` → (본문이 있으면) `REQ_BODY`… → `REQ_END`.
- agent 는 `RES_HEAD` → `RES_BODY`… → `RES_END` 로 응답한다. 헤더 수신 전에 상태를 알 수 없으면 `502` 로 응답한다.
- 헤더 규칙
  - relay→agent: 홉바이홉 헤더 제거(`connection, keep-alive, proxy-*, te, trailer, transfer-encoding, upgrade, sec-websocket-*`), `host` 는 릴레이가 `127.0.0.1`(env `LAF_RELAY_TARGET_HOST`)로 재작성(공개 호스트명이 앱에 새지 않도록), `X-LAF-Client-IP: <클라이언트 IP>` 추가. 에이전트는 `host` 를 홉바이홉으로 취급해 **자기 로컬 대상(`127.0.0.1:<PORT+1>`)으로 다시 설정**한다 → 앱이 보는 값은 항상 로컬 주소다.
  - agent→relay: 홉바이홉 헤더 제거, `content-length` 는 relay 가 재계산(청크 전송이면 생략).
  - `Authorization` / `Cookie` / `User-Agent` 는 변형 없이 통과한다.
- 본문: 최대 **8 MiB**. 초과 시 relay 는 스트림을 `CANCEL{reason:"too_large"}` 하고 클라이언트에 `413` 을 반환한다.
- 쿼리스트링은 `query` 필드로 그대로 전달한다(`path` 와 분리).

## 5. 한계값 (v1)

| 항목 | 값 | 초과 시 |
| --- | --- | --- |
| 호스트당 동시 스트림 | 16 | relay → 클라이언트 `429` |
| 요청 본문 | 8 MiB | `413` |
| 프레임 크기 | 1 MiB | 세션 종료 |
| 스트림 무진행 | 60초 | `CANCEL` + 클라이언트 `504` |
| 헤더 대기(응답 헤더) | 30초 | `CANCEL` + `504` |
| WS 무응답 | 60초 | 세션 종료 |

**스트리밍 응답(SSE)**: 에이전트는 응답을 조각 단위로 흘려보내므로 `text/event-stream` 같은
장기 응답이 그대로 통과한다. 다만 위 **스트림 무진행 60초**에 걸리지 않도록 앱은 하트비트를
60초보다 짧게 보내야 한다(현재 `/api/events` 는 15초). 에이전트의 청크 간 대기 상한은 30초이며,
로컬 앱과는 **HTTP/1.1 keep-alive 연결 풀**(요청당 새 연결을 열지 않음)로 통신한다.

## 6. 오류 응답(릴레이 자체)

| 상황 | 응답 |
| --- | --- |
| `host_id` 미접속(오프라인) | `503` + `{"detail":"host offline"}` |
| `host_id` 형식 오류 | `404` |
| 동시 스트림 초과 | `429` |
| 릴레이 내부 오류 | `502` |

## 7. 운영

- `GET /healthz` → `{"ok":true,"hosts":N,"sessions":N,"streams":N}` (인증 없음, 본문 없음).
- 레지스트리(`host_id` → `token_hash`)는 파일에 원자적으로 저장한다(재시작 후에도 선점 방지 유지).
- 로그는 메타데이터만(시각, host_id, 메서드, 경로, 상태, 바이트). 본문/파일은 저장하지 않는다.
- TLS 는 Caddy(Let's Encrypt 자동) 담당. relayd 는 `127.0.0.1:8788` 에만 바인딩한다.

## 8. 버전 관리

- 프로토콜 변경은 이 문서를 먼저 고친다. `HELLO.version` 은 정수 문자열이며 불일치 시 명시적으로 거부한다.

## 9. 종단간 암호화 (E2E v2)

릴레이(그리고 그 앞단의 TLS 종단)가 **내용을 읽을 수 없게** 하려면 앱과 폰 사이에 별도 암호 계층이
필요하다. 릴레이는 `POST /h/<host_id>/api/e2e` 의 **불투명한 본문**만 중계하며, 경로·헤더·본문·
기기 토큰은 모두 암호문 안에 있다. 프로토콜 프레임(v1)은 그대로이며 앱 내부 구현만 확장한다.

### 9.1 알고리즘
- 키 합의: **ECDH P-256**, KDF: **HKDF-SHA256**, AEAD: **AES-256-GCM**.
- 앱 키쌍은 최초 1회 생성해 앱 설정(`app_settings`)에 보관한다. 공개키는 SPKI(DER) base64url.

### 9.2 페어링(암호화된 claim)
- 페어링 링크/QR 에 앱 공개키를 싣는다: `...&pk=<base64url SPKI>`.
- 폰은 자기 키쌍을 만들고 `shared = ECDH(phone_priv, app_pub)`,
  `pair_key = HKDF(ikm=shared, salt=<페어링 토큰 UTF-8>, info="lafhvst-pair-v2", 32)`.
- `enc` 필드 = `base64url( nonce(12) || AES-256-GCM(pair_key, nonce, plaintext, aad) )`,
  aad = `b"lafhvst-pair-v2|req|"`(요청) / `b"lafhvst-pair-v2|res|"`(응답). nonce 는 매번 무작위.
- `POST /api/pair/claim` 본문: `{"v":2,"pk":"<폰 공개키 b64url>","enc":"<enc>"}`,
  plaintext = `{"t":"<페어링 토큰>","name":"<기기 이름>"}`.
  서버는 **활성 페어링 토큰을 salt 로** 복호화를 시도한다(실패 시 401).
- 응답 본문: `{"v":2,"enc":"<enc>"}`,
  plaintext = `{"device_id","device_token","host_id","public_url","name"}`
  → 릴레이는 기기 토큰을 볼 수 없다.
- 서버는 기기 행에 폰 공개키를 저장한다(`remote_devices.public_key`).
- v1(평문 claim)은 하위 호환으로 유지한다: `v`/`enc` 가 없으면 기존 경로.

### 9.3 세션
- `session_key = HKDF(ikm=ECDH(앱 priv, 폰 pub), salt=b"lafhvst-e2e-v1", info="device:<device_id>", 32)`.
- **요청**(폰→앱): `POST /api/e2e`
  `body = 0x02 | device_id(4 BE) | nonce(12) | AES-256-GCM(session_key, nonce, plaintext, aad=b"lafhvst-e2e-v1|req|" + device_id)`
  plaintext(JSON) = `{"method","path","query","headers","body_b64"}`.
- **응답**(앱→폰): HTTP 200, `body = 0x02 | nonce(12) | ct`,
  plaintext(JSON) = `{"status","headers","body_b64"}`,
  aad = `b"lafhvst-e2e-v1|res|" + device_id`.
- `nonce(12) = counter(8 BE) || direction(1) || 0x000000`. 요청 카운터는 폰이, 응답 카운터는 앱이
  각각 0부터 증가시키며 **재사용하지 않는다**.
- 재전송 방지: 앱은 기기별로 마지막 요청 카운터를 저장하고 `counter <= last` 인 봉투를 거부한다(401).
- 크기: 봉투 본문 ≤ 8 MiB(릴레이 상한), `body_b64` 디코드 결과 ≤ 6 MiB.

### 9.4 스트리밍 제외
SSE(`/api/events`)는 단일 봉투로 감쌀 수 없다(스트리밍). E2E 를 쓰는 기기는 **폴링**
(`/api/dashboard/live`)으로 전환한다. 즉 E2E 활성 시 실시간 푸시 대신 주기 조회를 사용한다.

### 9.5 공개 경로
`/api/e2e` 와 `/api/pair/claim` 은 미들웨어 허용목록에 포함된다(인증은 **봉투 내부**에서 한다).
`/api/e2e` 응답은 항상 200 이며 실제 상태 코드는 암호문 안에 들어 있다.
