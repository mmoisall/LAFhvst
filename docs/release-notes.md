## 하이라이트 (v0.2.0)

### 원격 제어 (신규)

- **포트포워딩 없이 QR/페어링 링크로 휴대폰에서 제어**: PC가 자체 릴레이(`relay/`)에 아웃바운드 `wss`로
  연결하고, 폰은 고정된 공개 주소로 접속합니다. 공유기 설정·고정 IP·DDNS가 필요 없습니다.
- **기기 토큰 인증**: 페어링 토큰은 5분 만료·1회용이며 등록 성공 시 자동 회전합니다. 기기 목록과
  회수(revoke)를 설정 화면에서 관리합니다.
- **권한 스코프**: 기본 원격 권한은 조회+제어. 파괴적 작업(삭제/이동/초기화/로그 비우기)은
  **원격 파괴적 작업 허용**을 켜야 가능합니다. `reveal`(탐색기 열기)과 프로파일 로그인 실행은
  데스크톱 전용입니다.
- **실행 중 수집 정지**: 홈 작업 큐와 DB 소스 행의 `■` 버튼으로 취소할 수 있습니다(취소는 실패로
  기록되지 않습니다).
- **실시간 갱신(SSE)**: `GET /api/events`가 대시보드 변화와 감시 이벤트를 한 연결로 밀어줍니다.
- **앱처럼 설치(PWA)**: 릴레이(https) 접속 시 홈 화면에 추가할 수 있습니다. 서비스 워커는 셸과
  400px 썸네일만 캐시하고 API 응답은 캐시하지 않습니다.
- **알림(웹훅)**: 수집 완료/실패를 ntfy·Discord·Slack·Telegram 또는 일반 JSON/텍스트 엔드포인트로
  받을 수 있습니다(중복 억제, 테스트 전송 버튼 제공).
- **종단간 암호화(E2E)**: 릴레이(및 그 앞단의 TLS 종단)는 **불투명한 봉투**만 중계합니다 — 요청
  경로·헤더·기기 토큰이 모두 AES-256-GCM 암호문 안에 있습니다. 키는 페어링 시 기기별로 합의하며
  (ECDH P-256 + HKDF-SHA256), 앱 공개키는 QR/페어링 링크로 전달됩니다. 재전송된 봉투는 거부됩니다.
  E2E 사용 중에는 실시간 푸시(SSE) 대신 폴링합니다.

### ⚠️ 호환성 변화 (중요)

- **인증 도입으로 원격 접근 정책이 바뀌었습니다.** 이전에는 같은 네트워크(LAN)에서 인증 없이
  접속할 수 있었지만, 이제 loopback(데스크톱 창)을 제외한 **모든 요청에 페어링된 기기 토큰이
  필요**합니다.
  - 데스크톱 앱 창은 영향을 받지 않습니다.
  - LAN 브라우저·스크립트로 쓰던 경우: **설정 → 원격 접속 → 원격 접속 켜기 → QR/페어링 링크**로
    기기를 등록한 뒤 `Authorization: Bearer <기기 토큰>`을 붙여야 합니다.
- 원격에서 썸네일은 400px 축소본으로만 제공됩니다.

### 설치 / 업데이트

- zip을 풀고 실행하면 데이터(`data/`, `lafhvst.db`, `log/`)는 기존과 동일하게 보존됩니다.
- **릴레이 서버는 앱 zip에 포함되지 않습니다.** 별도 배포가 필요합니다 → `relay/README.md`
  (VPS + 도메인 + Caddy TLS + systemd).
- 신규 런타임 의존성: `segno`(QR 생성), `websockets`(릴레이 연결).

### English summary

- **Remote control without port forwarding**: the desktop app opens an outbound `wss` tunnel to your
  own relay (`relay/`); phones connect to a stable public URL and pair via QR/pairing link.
- **Breaking change**: every non-loopback request (LAN **and** relay) now requires a paired device
  token. The desktop window is unaffected. Register a device under Settings → 원격 접속.
- Destructive actions are opt-in for remote devices; `reveal` and profile login launch stay
  desktop-only. Running collections can be stopped from the phone.
- Adds SSE live updates (`/api/events`), PWA install over the relay, and webhook notifications.
- **End-to-end encryption**: the relay only forwards opaque envelopes — paths, headers and the device
  token stay inside an AES-256-GCM payload (ECDH P-256 + HKDF-SHA256, keys negotiated during pairing).
  Replayed envelopes are rejected; SSE is replaced by polling while E2E is active.
- The relay server is **not** shipped in the app zip — deploy it separately (`relay/README.md`).
