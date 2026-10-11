"""로컬 연동 테스트 러너 — 릴레이 + 앱(에이전트) + '폰' 을 한 프로세스에서 띄워 실제 경로를 검증한다.

검증 대상(실제 배포와 같은 코드 경로):
  1) 릴레이 공개 주소 → 에이전트 → 원격 리스너 → 앱 프록시
  2) v1(평문) 페어링/Bearer 경로와 권한 스코프
  3) E2E v2 암호화 페어링 + 세션 봉투(재전송 거부, 봉투 평문 비노출)
  4) SSE 스트리밍(릴레이 통과 시 버퍼링되지 않음)
  5) 웹훅 알림 전달
  6) disable 시 공개 엔드포인트 소멸

사용:
    python tests/integration/local_relay.py            # 검증 후 종료(성공 시 exit 0)
    python tests/integration/local_relay.py --keep     # 검증 후 서버 유지(폰/브라우저 수동 테스트)
    python tests/integration/local_relay.py --keep --host 0.0.0.0   # 같은 네트워크 폰에서 접속

`--keep` 은 페어링 링크/공개 주소/기기 토큰을 출력한다. 브라우저로 열어 수동 확인에 쓴다.
(`frontend/js/e2e.js` 를 쓰는 Node 폰 검사는 `tests/integration/phone_webcrypto.js` 참고)
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# 콘솔 인코딩이 cp1252 등이어도 한글 출력이 깨지지 않게 한다(파이프/서비스 실행 포함).
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

APP_PORT = 17470
RELAY_PORT = 8788

failures: list[str] = []
checks = 0
_conns: dict[tuple[str, int], http.client.HTTPConnection] = {}


# --- 도우미 ---------------------------------------------------------------
def check(label: str, actual, expected) -> None:
    global checks
    checks += 1
    if actual == expected:
        print("  PASS %s" % label)
    else:
        print("  FAIL %s: %r != %r" % (label, actual, expected))
        failures.append(label)


def check_true(label: str, value) -> None:
    check(label, bool(value), True)


def call(host_port, method, path, *, token=None, body=None, retries=1):
    """keep-alive 연결을 재사용해 JSON 요청을 보낸다(간헐 RST 대비 1회 재시도)."""
    for attempt in range(retries + 1):
        conn = _conns.get(host_port)
        if conn is None:
            conn = http.client.HTTPConnection(host_port[0], host_port[1], timeout=30)
            _conns[host_port] = conn
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            _conns.pop(host_port, None)
            if attempt == retries:
                raise
    raise RuntimeError("unreachable")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(predicate, timeout=30.0, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


class Receiver:
    """웹훅 수신기."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                outer.bodies.append(self.rfile.read(length) if length else b"")
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/hook" % self.port

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def read_sse(host_port, path, token, timeout=20.0):
    """raw 소켓으로 SSE 첫 프레임을 읽는다(chunked 디코딩 포함)."""
    sock = socket.create_connection(host_port, timeout=timeout)
    sock.settimeout(timeout)
    sock.sendall(
        (
            "GET %s HTTP/1.0\r\nHost: %s:%d\r\nAuthorization: Bearer %s\r\n"
            "Accept: text/event-stream\r\nConnection: close\r\n\r\n"
            % (path, host_port[0], host_port[1], token)
        ).encode()
    )
    raw = b""
    text = ""
    chunked = False
    headers_done = False
    started = time.monotonic()
    try:
        while time.monotonic() - started < timeout:
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw += chunk
            if not headers_done:
                marker = raw.find(b"\r\n\r\n")
                if marker < 0:
                    continue
                head = raw[:marker].decode("latin-1")
                chunked = "transfer-encoding: chunked" in head.lower()
                raw = raw[marker + 4 :]
                headers_done = True
            if chunked:
                while True:
                    payload, raw = _take_chunk(raw)
                    if payload is None or payload == b"":
                        break
                    text += payload.decode("utf-8", "replace")
            else:
                text += raw.decode("utf-8", "replace")
                raw = b""
            if "event: dashboard" in text:
                return text
    finally:
        sock.close()
    return text


def _take_chunk(buffer: bytes):
    index = buffer.find(b"\r\n")
    if index < 0:
        return None, buffer
    try:
        size = int(buffer[:index].split(b";")[0].strip() or b"0", 16)
    except ValueError:
        return None, buffer
    if size == 0:
        return b"", b""
    end = index + 2 + size
    if len(buffer) < end + 2:
        return None, buffer
    return buffer[index + 2 : end], buffer[end + 2 :]


# --- 서버 기동 -------------------------------------------------------------
def start_relay(registry_path: str, host: str):
    os.environ["LAF_RELAY_REGISTRY"] = registry_path
    import logging

    import uvicorn

    from relay import relayd

    level = os.environ.get("LAF_RELAY_LOG", "warning")
    logging.basicConfig(level=getattr(logging, level.upper(), logging.WARNING), force=True)
    config = uvicorn.Config(
        relayd.app, host=host, port=RELAY_PORT, log_level=level, timeout_keep_alive=75
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, name="relay", daemon=True).start()
    return server


def start_app(host: str, relay_base: str, tmp_dir: str):
    os.environ["LAF_DB_PATH"] = os.path.join(tmp_dir, "lafhvst.db")
    os.environ["LAF_NO_GUI"] = "1"
    import uvicorn

    import server
    from core import database

    database.init_db()
    server.SERVER_PORT = APP_PORT
    server.SETTINGS.update(
        {"remoteEnabled": True, "remoteRelayUrl": relay_base, "remoteAutoStart": True}
    )
    config = uvicorn.Config(
        server.app,
        host=host,
        port=APP_PORT,
        log_level="warning",
        loop="server:proactor_loop_factory",
    )
    instance = uvicorn.Server(config)
    threading.Thread(target=instance.run, name="app", daemon=True).start()
    return instance, server


# --- 검증 -----------------------------------------------------------------
def run_checks(server_module, relay_base: str, host: str) -> dict:
    from core import remote_crypto

    app = (host if host != "0.0.0.0" else "127.0.0.1", APP_PORT)
    relay = (host if host != "0.0.0.0" else "127.0.0.1", RELAY_PORT)

    print("[1] 릴레이/에이전트 연결")
    check("relay healthz", call(relay, "GET", "/healthz")[0], 200)
    check_true(
        "agent connected",
        wait_for(
            lambda: json.loads(call(app, "GET", "/api/remote/status")[1]).get("running"), timeout=30
        ),
    )
    status = json.loads(call(app, "GET", "/api/remote/status")[1])
    host_id = status["host_id"]
    prefix = "/h/" + host_id
    check("public url uses relay", status["url"].startswith(relay_base), True)

    print("[2] 공개 경로/미인증 차단")
    check("index via relay", call(relay, "GET", prefix + "/")[0], 200)
    _s, _b = call(relay, "GET", prefix + "/pair.html")
    check("pair.html via relay", _s, 200)
    if _s != 200:
        print("    body: %r" % _b[:200])
    _s, _b = call(relay, "GET", prefix + "/api/version")
    check("api/version via relay", _s, 200)
    if _s != 200:
        print("    body: %r" % _b[:200])
    check("unpaired api 401", call(relay, "GET", prefix + "/api/settings")[0], 401)
    check("bad host 404", call(relay, "GET", "/h/short/api/version")[0], 404)
    check("unknown host 503", call(relay, "GET", "/h/zzzzzzzzzzzzzzzzzzzz/api/version")[0], 503)

    print("[3] v1(평문) 페어링 + 권한 스코프")
    pair = status["pair"]
    check("pair link has app key", "pk=" in pair["link"], True)
    v1_token = pair["link"].split("#t=")[1].split("&")[0]
    status_code, raw = call(relay, "POST", prefix + "/api/pair/claim", body={"t": v1_token, "name": "v1 폰"})
    check("v1 claim", status_code, 200)
    v1_device = json.loads(raw)["device_token"]
    check("v1 settings", call(relay, "GET", prefix + "/api/settings", token=v1_device)[0], 200)
    check(
        "destructive denied",
        call(relay, "POST", prefix + "/api/explorer/batch-delete", token=v1_device, body={"items": []})[0],
        403,
    )
    check(
        "stop endpoint",
        call(relay, "POST", prefix + "/api/sources/999999/stop", token=v1_device, body={})[0],
        200,
    )

    print("[4] E2E v2 암호화 페어링 + 세션 봉투")
    status = json.loads(call(app, "GET", "/api/remote/status")[1])
    pair = status["pair"]
    check("e2e enabled", pair["e2e"], True)
    e2e_token = pair["link"].split("#t=")[1].split("&")[0]
    app_public = pair["app_public_key"]
    private, public = remote_crypto.generate_keypair()
    key = remote_crypto.pair_key(private, app_public, e2e_token)
    payload = remote_crypto.encode_pair_request(key, public, e2e_token, "E2E 폰")
    status_code, raw = call(relay, "POST", prefix + "/api/pair/claim", body=payload)
    check("v2 claim", status_code, 200)
    inner = remote_crypto.decode_pair_response(key, json.loads(raw))
    device_id = int(inner["device_id"])
    device_token = inner["device_token"]
    check_true("v2 device token", len(device_token) > 20)

    session = remote_crypto.session_key(private, app_public, device_id)
    counter = 0

    def envelope(method, path, body=b"", use_counter=None):
        nonlocal counter
        value = counter if use_counter is None else use_counter
        counter = max(counter, value + 1)
        headers = {"authorization": "Bearer " + device_token, "accept": "application/json"}
        if body:
            headers["content-type"] = "application/json"
        return remote_crypto.encode_request(
            device_id, value, session, method=method, path=path, headers=headers, body=body
        )

    def e2e_call(blob):
        conn = _conns.get(relay)
        if conn is None:
            conn = http.client.HTTPConnection(relay[0], relay[1], timeout=30)
            _conns[relay] = conn
        conn.request(
            "POST",
            prefix + "/api/e2e",
            body=blob,
            headers={"Content-Type": "application/octet-stream"},
        )
        response = conn.getresponse()
        payload = response.read()
        return response.status, payload

    status_code, blob = e2e_call(envelope("GET", "/api/settings"))
    check("e2e request", status_code, 200)
    decoded = remote_crypto.decode_response(device_id, session, blob)
    check("e2e inner status", decoded["status"], 200)
    check_true("e2e settings body", b"remoteEnabled" in decoded["body"])

    status_code, blob = e2e_call(envelope("POST", "/api/explorer/batch-delete", b'{"items":[]}'))
    decoded = remote_crypto.decode_response(device_id, session, blob)
    check("e2e scope denied", decoded["status"], 403)

    replay = envelope("GET", "/api/version")
    check("e2e first send", e2e_call(replay)[0], 200)
    check("e2e replay rejected", e2e_call(replay)[0], 401)

    opaque = envelope("GET", "/api/version")
    check_true("envelope hides path", b"/api/" not in opaque)
    check_true("envelope hides token", device_token.encode() not in opaque)

    print("[5] SSE 스트리밍(릴레이 통과)")
    text = read_sse(relay, prefix + "/api/events", device_token)
    check_true("sse dashboard event", "event: dashboard" in text)

    print("[6] 웹훅 알림")
    receiver = Receiver()
    try:
        call(app, "POST", "/api/settings", body={"notifyWebhookUrl": receiver.url, "notifyPreset": "json"})
        status_code, raw = call(relay, "POST", prefix + "/api/notify/test", token=device_token, body={})
        check("notify test via relay", status_code, 200)
        notify_result = json.loads(raw) if raw else {}
        check_true("notify ok", notify_result.get("ok"))
        if not notify_result.get("ok"):
            print("    notify 응답: %r" % notify_result)
        check_true("webhook delivered", wait_for(lambda: len(receiver.bodies) >= 1, timeout=10))
        if receiver.bodies:
            check("webhook app field", json.loads(receiver.bodies[0].decode())["app"], "LAFhvst")
        call(app, "POST", "/api/settings", body={"notifyWebhookUrl": ""})
    finally:
        receiver.close()

    print("[7] 기기 회수 / 원격 접속 끄기")
    check("revoke v1 device", call(app, "DELETE", "/api/remote/devices/%d" % json.loads(
        call(app, "GET", "/api/remote/status")[1]
    )["devices"][0]["id"])[0], 200)
    check(
        "revoked device 401",
        call(relay, "GET", prefix + "/api/settings", token=v1_device)[0],
        401,
    )
    check("disable", call(app, "POST", "/api/remote/disable", body={})[0], 200)
    check_true("endpoint gone", wait_for(lambda: call(relay, "GET", prefix + "/api/version")[0] == 503, timeout=15))

    return {"host_id": host_id, "device_token": device_token, "device_id": device_id, "prefix": prefix}


def main() -> int:
    parser = argparse.ArgumentParser(description="LAFhvst 로컬 연동 테스트")
    parser.add_argument("--host", default="127.0.0.1", help="바인딩 호스트(폰 테스트는 0.0.0.0)")
    parser.add_argument("--keep", action="store_true", help="검증 후 서버를 유지한다(수동 테스트용)")
    parser.add_argument("--no-checks", action="store_true", help="--keep 과 함께: 검증 없이 서버만 띄운다")
    args = parser.parse_args()

    tmp_dir = tempfile.mkdtemp(prefix="lafhvst-integration-")
    relay_base = "http://%s:%d" % ("127.0.0.1" if args.host == "0.0.0.0" else args.host, RELAY_PORT)
    print("임시 데이터: %s" % tmp_dir)

    relay_server = start_relay(os.path.join(tmp_dir, "registry.json"), args.host)
    app_server, server_module = start_app(args.host, relay_base, tmp_dir)
    if not wait_for(lambda: call(("127.0.0.1", APP_PORT), "GET", "/api/version")[0] == 200, timeout=30):
        print("앱 기동 실패")
        return 1

    info = {}
    if not args.no_checks:
        try:
            info = run_checks(server_module, relay_base, args.host)
        except Exception as exc:  # noqa: BLE001
            print("검증 중 예외: %r" % exc)
            failures.append("exception: %r" % exc)

    print("")
    if failures:
        print("실패 %d / 검사 %d: %s" % (len(failures), checks, ", ".join(failures)))
    else:
        print("통과 %d/%d" % (checks, checks))

    if args.keep:
        print("")
        print("서버 유지 중(중지: Ctrl+C)")
        print("  릴레이: %s" % relay_base)
        print("  앱(loopback): http://127.0.0.1:%d" % APP_PORT)
        if info.get("host_id"):
            print("  공개 주소: %s/h/%s" % (relay_base, info["host_id"]))
            print("  기기 토큰: %s" % info.get("device_token"))
        if args.host == "0.0.0.0":
            from core.utils import local_ipv4_addresses

            for address in local_ipv4_addresses():
                print("  같은 네트워크 폰: http://%s:%d/pair.html  (릴레이 %s)" % (address, APP_PORT, relay_base))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    relay_server.should_exit = True
    app_server.should_exit = True
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
