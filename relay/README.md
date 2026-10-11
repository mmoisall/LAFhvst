# LAFhvst 릴레이 (relay)

포트포워딩 없이 원격 제어하기 위한 **자체 릴레이 서버**다. PC(에이전트)가 여는
아웃바운드 WebSocket 터널을 받아, 폰의 HTTPS 요청을 그 터널로 멀티플렉싱한다.

계약(단일 진실 원천)은 [`protocol.md`](protocol.md) 이며, PC 측 구현은
`core/remote_tunnel.py`(`RelayAgent`)다. **문서와 코드가 어긋나면 문서가 기준**이다.

```
[폰 PWA] --HTTPS--> Caddy(TLS) --> relayd(127.0.0.1:8788) --WS /agent--> RelayAgent(PC) --HTTP--> 127.0.0.1:PORT+1
```

앱(`server.py`/`core/*`)과 **별개 배포물**이다. 이 디렉터리만 있으면 돌아가고,
앱 코드는 릴레이를 import 하지 않는다.

## 1. 구성 요소

| 파일 | 역할 |
| --- | --- |
| `relayd.py` | ASGI 앱(`app`). `/healthz`, `WS /agent`, `/h/{host_id}/...` 프록시 |
| `mux.py` | WS 세션/스트림 멀티플렉서(프레이밍, 한계값, CANCEL/백프레셔) |
| `registry.py` | `host_id → sha256(agent_token)` 영속 저장(원자적 쓰기) |
| `deploy/Caddyfile` | TLS 종단 + WS 업그레이드 + 8788 루프백 프록시 |
| `deploy/lafhvst-relay.service` | systemd 유닛(최소 권한, 루프백 바인딩) |
| `tests/` | 실제 uvicorn + 가짜 에이전트(websockets) 통합 테스트 |

## 2. 요구사항

- Python 3.11+ (3.12 권장)
- `relay/requirements.txt` — fastapi, uvicorn[standard] (websockets 포함)
- 공인 도메인 1개(`relay.<도메인>`)와 공인 IP, 80/443 개방
- 릴레이는 **인증을 종단하지 않는다.** `Authorization`/`Cookie` 는 그대로 전달하고
  권한 판정은 앱의 원격 리스너가 한다.

## 3. 설치와 실행

```bash
# 저장소 루트에서
cd relay
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 개발/수동 실행 (릴레이는 반드시 루프백에만 바인딩)
cd ..
uvicorn relay.relayd:app --host 127.0.0.1 --port 8788

# 확인
curl -s http://127.0.0.1:8788/healthz
# {"ok":true,"hosts":0,"sessions":0,"streams":0}
```

운영에서는 `deploy/lafhvst-relay.service` 를 쓴다(§6).

### 환경변수

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `LAF_RELAY_REGISTRY` | `relay/data/registry.json` | 레지스트리 파일 경로(원자적 저장) |
| `LAF_RELAY_MAX_BODY` | `8388608` (8 MiB) | 요청 본문 상한. 초과 시 CANCEL + `413` |
| `LAF_RELAY_MAX_FRAME` | `1048576` (1 MiB) | 프레임 페이로드 상한. 초과 시 세션 종료 |
| `LAF_RELAY_MAX_STREAMS` | `16` | 호스트당 동시 스트림. 초과 시 `429` |
| `LAF_RELAY_HEADER_TIMEOUT` | `30` | 응답 헤더 대기. 초과 시 CANCEL + `504` |
| `LAF_RELAY_STREAM_IDLE_TIMEOUT` | `60` | 스트림 무진행. 초과 시 CANCEL + `504` |
| `LAF_RELAY_PING_INTERVAL` / `LAF_RELAY_PING_TIMEOUT` | `20` / `60` | PING 주기 / 무응답 종료 |
| `LAF_RELAY_HELLO_TIMEOUT` | `30` | HELLO 프레임 대기. 초과 시 `HELLO_ERR{code:"bad_hello"}` |
| `LAF_RELAY_QUEUE_MAX` | `32` | 스트림별 프레임 큐 상한. 초과 시 해당 스트림만 CANCEL(세션 유지) |
| `LAF_RELAY_CHUNK_SIZE` | `262144` | REQ_BODY 프레임당 최대 페이로드 |
| `LAF_RELAY_TARGET_HOST` | `127.0.0.1` | 에이전트로 보낼 `host` 헤더 값(§7 주의) |

관리자 토큰/계정 같은 것은 없다(레지스트리 = `host_id` 선점 방지용 해시 파일).

## 4. 레지스트리

- 최초 HELLO 가 `host_id` 를 **선점**하고 `sha256(agent_token)` 만 저장한다(평문 없음).
- 이후 접속은 `hmac.compare_digest` 상수시간 비교로 검증하며, 불일치는
  `HELLO_ERR{code:"host_id_taken"}` 로 거부한다.
- `host_id` 형식은 `[A-Za-z0-9_-]{16,}`(앱이 110bit 이상 랜덤으로 생성).
- 저장은 같은 디렉터리 임시 파일 + `os.replace` 로 원자적이며, 파일은 `0600` 이다.
- 파일이 손상되면 릴레이는 기동을 거부한다(재바인딩으로 인한 탈취 방지).
  복구하려면 파일을 백업에서 되돌리거나, 해당 호스트를 앱에서 재페어링해야 한다.
- **이 파일을 잃으면** 모든 PC 가 다음 접속에서 재바인딩된다(토큰이 그대로면 재선점만).

## 5. 배포 (도메인 / TLS / 방화벽)

1. DNS: `relay.<도메인>` A/AAAA 레코드를 릴레이 서버 공인 IP 로 지정한다.
2. Caddy 설치 후 `deploy/Caddyfile` 을 `/etc/caddy/Caddyfile` 로 복사하고
   `relay.example.com` 을 실제 도메인으로 바꾼다.
   - `reverse_proxy` 가 WebSocket 업그레이드를 자동 처리한다.
   - `flush_interval -1` 로 응답 스트리밍 버퍼링을 끈다(폰에서 체감 지연 방지).
   - `header_up X-Forwarded-For {http.request.remote.host}` 로 실제 클라이언트 IP 를
     넘기고, systemd 유닛의 `--proxy-headers --forwarded-allow-ips 127.0.0.1` 이 이를
     신뢰한다. 그래야 릴레이가 `X-LAF-Client-IP` 에 실제 IP 를 넣는다.
3. `sudo systemctl reload caddy` — Let's Encrypt 인증서가 자동 발급된다.
4. 방화벽: **80/443 만 공개**한다. `8788` 과 앱의 원격 리스너 포트는 열지 않는다
   (릴레이는 `127.0.0.1` 에만 바인딩).
5. Caddy 가 80/443 을 점유하므로 릴레이 서버에서 다른 웹서버를 함께 쓰지 않는다.

### 인증서 갱신

Caddy 가 만료 30일 전부터 자동 갱신한다(별도 cron 불필요).
`journalctl -u caddy` 로 실패 여부만 확인하면 되고, 80 포트가 막히면 HTTP-01 챌린지가
실패하므로 방화벽 규칙을 점검한다.

## 6. systemd

```bash
sudo mkdir -p /opt/lafhvst-relay && sudo rsync -a ./ /opt/lafhvst-relay/
cd /opt/lafhvst-relay && sudo python3 -m venv .venv
sudo .venv/bin/pip install -r relay/requirements.txt
sudo useradd --system --home /opt/lafhvst-relay --shell /usr/sbin/nologin lafrelay
sudo mkdir -p /var/lib/lafhvst-relay && sudo chown lafrelay:lafrelay /var/lib/lafhvst-relay
sudo cp relay/deploy/lafhvst-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lafhvst-relay
systemctl status lafhvst-relay
journalctl -u lafhvst-relay -f
```

로그는 메타데이터만 남긴다(시각, host_id, 메서드, 경로, 상태, 바이트). **본문/헤더 값은
저장하지 않는다.** uvicorn 액세스 로그는 `--log-level info` 로 접근 로그를 포함한다.

## 7. 운영 시 주의

- 앱(폰 PWA)의 릴레이 주소는 `https://relay.<도메인>/h/<host_id>/...` 이다.
  경로 프리픽스 `/h/<host_id>` 는 제거되고 나머지 경로와 쿼리가 그대로 전달된다.
- `host` 헤더는 릴레이가 공개 도메인 대신 루프백(`LAF_RELAY_TARGET_HOST`)으로
  재작성한다. 에이전트는 자기가 아는 로컬 대상(`127.0.0.1:<PORT+1>`)으로 다시 쓴다.
  릴레이는 에이전트의 로컬 포트를 모르므로 여기서 포트를 확정할 수 없다.
- `X-LAF-Client-IP` 는 Caddy 를 신뢰하는 uvicorn 프록시 헤더 처리 결과(=실제 클라이언트
  IP)이며, 클라이언트가 보낸 값은 항상 덮어쓴다.
- 상태 코드: 미접속 `503`, `host_id` 형식 오류 `404`, 동시 스트림 초과 `429`,
  본문 초과 `413`, 무진행/헤더 대기 초과 `504`, 내부 오류 `502`.
- 재연결: 같은 `host_id` 로 새 세션이 인증되면 기존 세션은 WS close `4000`(`replaced`)
  로 닫힌다. 앱 재시작을 흡수하기 위한 정상 동작이다.

## 8. 테스트

```bash
cd relay
python -m pytest tests -q     # 로컬 only, 외부 네트워크 접속 없음
```

테스트는 임의 포트로 실제 uvicorn(relayd)을 띄우고, `websockets` 동기 클라이언트로
`/agent` 에 붙는 **가짜 에이전트**를 에코 백엔드로 세운 뒤 httpx 로 프록시를 검증한다.

## 9. 트러블슈팅

| 증상 | 확인 |
| --- | --- |
| `/healthz` 가 `hosts:0, sessions:0` | PC 에이전트가 `wss://relay.<도메인>/agent` 로 붙었는지, 방화벽 443 아웃바운드 확인 |
| 모든 요청이 `503 host offline` | 에이전트 미접속/하트비트 종료(60초 무응답). `journalctl -u lafhvst-relay` 의 `heartbeat timeout` 확인 |
| `HELLO_ERR{code:"host_id_taken"}` | 앱에서 기기 재페어링(토큰 변경) 필요. 레지스트리를 되돌리지 말 것 |
| `HELLO_ERR{code:"version"}` | PC 앱과 릴레이 버전 불일치 — protocol.md §8 참고 |
| `429` 반복 | 앱이 동시 요청을 줄여야 함(호스트당 16) |
| `504` 반복 | 에이전트의 로컬 백엔드(`127.0.0.1:<PORT+1>`)가 응답하지 않음 |
| TLS 발급 실패 | 80 포트 개방 여부, 도메인 A 레코드, `journalctl -u caddy` |

> Windows 에서 개발용으로 실행하면 클라이언트 연결이 끊길 때
> `OSError: [WinError 10022]` 로그가 간헐적으로 찍힌다(proactor 이벤트 루프의 알려진 잡음, 동작에는
> 영향 없음). 운영 대상은 Linux 이며 이 로그는 나타나지 않는다.
