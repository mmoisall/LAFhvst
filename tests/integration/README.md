# 연동 테스트 (통합 검증)

릴레이 · 앱(에이전트) · 폰(WebCrypto) 을 실제로 붙여서 **배포와 같은 코드 경로**를 검증한다.
단위 테스트(`pytest tests`)와 달리 여기서는 실제 HTTP/WS 가 오간다.

## 준비

- Python 3.11+ (의존성: `pip install -r requirements.txt`, 개발용 `-r requirements-dev.txt`)
- Node 20+ (폰 검사용: `crypto.webcrypto` 와 전역 `fetch` 사용)

## 1. 로컬 연동 테스트 (한 명령)

```bash
python tests/integration/local_relay.py
```

한 프로세스에서 릴레이(8788) + 앱(17470) + 원격 리스너(17471)를 띄우고 다음을 검증한다(성공 시 exit 0).

| 단계 | 검증 내용 |
| --- | --- |
| 1 | 릴레이 `/healthz`, 에이전트 연결, 공개 주소가 릴레이 기준인지 |
| 2 | 공개 경로(정적/`/api/version`) 통과, 미인증 API 401, 잘못된 host_id 404 / 미등록 503 |
| 3 | v1(평문) 페어링 + Bearer, 파괴적 작업 403, 정지 엔드포인트 |
| 4 | E2E v2 페어링(공개키 포함), 세션 봉투 왕복, 스코프 403, 재전송 401, 봉투 평문 비노출 |
| 5 | SSE 스트리밍이 릴레이를 통과(버퍼링 없음) |
| 6 | 웹훅 알림 전달 |
| 7 | 기기 회수 즉시 401, 원격 접속 끄면 공개 엔드포인트 503 |

### 수동 확인용으로 띄워 두기

```bash
python tests/integration/local_relay.py --keep            # 검증 후 서버 유지 + 접속 정보 출력
python tests/integration/local_relay.py --keep --host 0.0.0.0   # 같은 네트워크 폰에서도 접속
python tests/integration/local_relay.py --keep --no-checks      # 검증 없이 서버만
```

`--keep` 은 공개 주소 · 기기 토큰 · (0.0.0.0 바인딩 시) 같은 네트워크 폰용 주소를 출력한다.
진단이 필요하면 `LAF_RELAY_LOG=info`(릴레이 로그) · `LAF_AGENT_DEBUG=1`(에이전트 프레임 로그)를 함께 준다.

## 2. 폰(WebCrypto) 검사 — 브라우저 없이 E2E 경로 검증

```bash
python tests/integration/local_relay.py --keep --no-checks     # 터미널 1
node tests/integration/phone_webcrypto.js                      # 터미널 2
```

`frontend/js/e2e.js`(브라우저와 같은 코드)를 Node 에서 실행해 **v2 암호화 페어링 → 세션 봉투 요청 →
재전송 거부 → 카운터 지속 → v1 하위 호환**을 확인한다. HTTP 레벨에 `Authorization` 헤더가 없고
봉투에 토큰/경로가 평문으로 없다는 것도 함께 검사한다.

대상 주소를 바꾸려면 인자로 넘긴다:

```bash
node tests/integration/phone_webcrypto.js http://127.0.0.1:17599 http://127.0.0.1:8788
```

## 3. 실제 폰으로 확인 (수동)

### 3-1. 같은 네트워크(LAN)에서 — 평문(v1) 경로

```bash
python tests/integration/local_relay.py --keep --host 0.0.0.0
```

폰 브라우저에서 `http://<PC IP>:17470/` 접속 → 설정 → 원격 접속 → QR/링크로 기기 등록 → 홈에서 수집 시작/정지.
**주의**: 평문 http 는 보안 컨텍스트가 아니라 `crypto.subtle` 을 쓸 수 없다 → E2E 가 자동으로 꺼지고 v1(Bearer) 로 동작한다(정상 폴백).

### 3-2. 외부 네트워크에서 — E2E(암호화) 경로

1. 릴레이를 인터넷에 배포: `relay/README.md` (VPS + 도메인 + Caddy TLS + systemd)
2. PC 앱: 설정 → 원격 접속 → 릴레이 주소(`https://relay.<도메인>`) 입력 → **원격 접속 켜기**
3. 폰: QR 스캔 → `https://relay.<도메인>/h/<host_id>/pair.html#t=...` → 페어링
4. 확인 항목
   - 홈 대시보드/작업 큐가 보이고 **정지(■)** 가 동작
   - 설정 → 원격 접속: 이 기기가 목록에 있고 회수하면 즉시 401(재페어링 필요)
   - 파괴적 작업(삭제/이동)은 옵트인 전에는 403
   - 폰 브라우저 개발자도구 → Network: `/api/e2e` 요청에 `Authorization` 헤더가 **없고** 본문이 암호문
   - 홈 화면에 추가(PWA) 후 재접속 시 페어링 유지

## 알려진 환경 특성(Windows)

- loopback 에 새 연결을 반복해 열 때 간헐적으로 RST(WinError 10054/10022)가 난다(수정 전 베이스라인
  앱에서도 동일). 그래서 **웹훅 전달 검사(6단계)가 드물게 실패**할 수 있다 — 실패하면 재실행하면 된다.
  앱의 에이전트 경로는 연결 재사용 + 1회 재시도로 이 문제를 흡수한다.
- 콘솔 인코딩이 cp1252 인 환경에서도 한글이 깨지지 않도록 러너가 stdout 을 UTF-8 로 재설정한다.

## 관련 테스트

| 대상 | 명령 |
| --- | --- |
| 앱 단위/통합 | `pytest tests -q` |
| 릴레이 서버 | `cd relay && pytest tests -q` |
| 폰 암호 모듈(벡터) | `node tests/e2e_vectors.test.js` |
| 로컬 연동(이 문서) | `python tests/integration/local_relay.py` |
| 폰 경유 E2E | `node tests/integration/phone_webcrypto.js` |
