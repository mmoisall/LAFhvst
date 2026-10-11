"""원격(릴레이/터널) 전용 리스너.

릴레이 에이전트는 이 리스너(`127.0.0.1:<port>`)만 바라본다. 여기서 모든 요청을
`remote_listener` 로 표시하고 `scope["client"]` 를 **비-loopback 으로 강제**한다.
그래야 `request.client.host` 를 보는 기존 코드(썸네일 축소본, reveal 가드)가
릴레이 경유 원격 사용자를 로컬로 오인하지 않는다.

신뢰 규칙(docs/remote-access-plan.md §3.1):
- 접근 허용 판정은 `remote_listener` 플래그 + 기기 토큰만으로 한다.
- 클라이언트가 보낸 `X-Forwarded-For` 는 신뢰하지 않는다.
- 실 IP 는 릴레이가 검증해 넣는 `X-LAF-Client-IP` 만 사용하고, 형식이 틀리면 버린다.
"""

from __future__ import annotations

import threading

import uvicorn

from . import remote_access

DEFAULT_IP_HEADER = b"x-laf-client-ip"
REMOTE_LOOPBACK_PLACEHOLDER = "0.0.0.0"


class RemoteOnly:
    """원격 리스너 전용 ASGI 래퍼."""

    def __init__(self, app, client_ip_header: bytes = DEFAULT_IP_HEADER) -> None:
        self.app = app
        self.client_ip_header = client_ip_header

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {key.lower(): value for key, value in scope.get("headers") or []}
            ip = remote_access.remote_client_ip(headers.get(self.client_ip_header))
            scope["client"] = (ip or REMOTE_LOOPBACK_PLACEHOLDER, 0)
            state = scope.setdefault("state", {})
            state["remote_listener"] = True
            state["remote_client_ip"] = ip or ""
        await self.app(scope, receive, send)


class RemoteListener:
    """`127.0.0.1:<port>` 에서 원격 전용 앱을 서빙하는 백그라운드 uvicorn 서버."""

    def __init__(self, app, host: str = "127.0.0.1") -> None:
        self.host = host
        self.app = RemoteOnly(app)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    @property
    def running(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    def start(self, port: int, *, wait: bool = True, timeout: float = 15.0) -> bool:
        if self.running:
            return True
        from server import wait_for_server  # 순환 import 회피(지연)

        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=port,
            log_level="warning",
            loop="server:proactor_loop_factory",
            # 같은 프로세스의 앱을 한 번 더 띄우는 것이므로 앱 lifespan 을 다시 돌리지 않는다.
            # (돌리면 자동 시작 훅이 중복 실행되어 에이전트가 2개 붙고 세션이 서로 대체된다)
            lifespan="off",
            # 에이전트가 keep-alive 연결을 재사용한다 → 유휴 타임아웃을 길게 잡아
            # "서버가 먼저 닫은 연결" 경합을 줄인다(기본 5초는 너무 짧다).
            timeout_keep_alive=75,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name="lafhvst-remote", daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        self.port = port
        if wait and not wait_for_server(port, host=self.host, timeout=timeout):
            self.stop()
            return False
        return True

    def stop(self, timeout: float = 5.0) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        self.port = 0
        if server is not None:
            try:
                server.should_exit = True
            except Exception:
                pass
        if thread is not None:
            try:
                thread.join(timeout=timeout)
            except Exception:
                pass
