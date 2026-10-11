"""LAFhvst 릴레이 서버 (ASGI 앱).

프로토콜: `relay/protocol.md` v1 (단일 계약). 앱(`server.py`)과 별개 배포물이다.

실행:
    uvicorn relay.relayd:app --host 127.0.0.1 --port 8788

- `GET /healthz` — 상태 JSON(인증 없음).
- `WS /agent` — PC 에이전트의 아웃바운드 터널.
- `ANY /h/{host_id}[/{path}]` — 폰 요청을 해당 세션으로 멀티플렉싱.

TLS 는 Caddy 가 담당한다(`relay/deploy/Caddyfile`). relayd 는 루프백에만 바인딩한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.requests import ClientDisconnect

from . import mux
from .mux import AgentSession, SessionHub, Stream, TooManyStreams
from .registry import Registry, is_valid_host_id

log = logging.getLogger("relay")

# 클라이언트 IP 를 신뢰하는 헤더 이름(앱의 core/remote_listener.py 와 동일).
CLIENT_IP_HEADER = "x-laf-client-ip"

# 에이전트가 붙일 로컬 대상. 릴레이는 에이전트의 실제 포트(PORT+1)를 모르므로
# 공개 호스트명이 앱에 새지 않도록 루프백으로만 재작성한다(README 참고).
TARGET_HOST = os.environ.get("LAF_RELAY_TARGET_HOST", "127.0.0.1")

# REQ_BODY / RES_BODY 프레임당 최대 페이로드(1 MiB 한계 이내).
CHUNK_SIZE = int(os.environ.get("LAF_RELAY_CHUNK_SIZE", 256 * 1024))

BODY_DETAIL = "request body too large"

# 홉바이홉 헤더 — relay→agent, agent→relay 양방향에서 제거한다(protocol.md §4).
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"]

app = FastAPI(title="LAFhvst relay", docs_url=None, redoc_url=None, openapi_url=None)
registry = Registry()
hub = SessionHub(registry)


# ── 유틸 ────────────────────────────────────────────────────────────────
def _is_hop_by_hop(name: str) -> bool:
    """홉바이홉(또는 WS 업그레이드 전용) 헤더인지."""
    return name in HOP_BY_HOP or name.startswith("proxy-") or name.startswith("sec-websocket-")


def _request_headers(request: Request) -> dict[str, str]:
    """릴레이→에이전트로 보낼 헤더 — 홉바이홉 제거, host 재작성, 클라이언트 IP 주입."""
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        key = name.lower()
        if _is_hop_by_hop(key):
            continue
        headers[key] = value
    headers["host"] = TARGET_HOST
    headers[CLIENT_IP_HEADER] = request.client.host if request.client else ""
    return headers


def _response_headers(stream: Stream) -> dict[str, str]:
    """에이전트→릴레이 응답 헤더 — 홉바이홉 제거, content-length 는 재계산(생략)."""
    headers = {
        name: value
        for name, value in stream.res_headers.items()
        if not _is_hop_by_hop(name) and name != "content-length"
    }
    return headers


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _declares_body(request: Request) -> bool:
    if request.headers.get("transfer-encoding"):
        return True
    length = _content_length(request)
    return bool(length)


def _log_line(
    host_id: str, request: Request, stream: Stream, status: int, size: int, started: float
) -> None:
    """메타데이터만 남긴다(본문/헤더 값 없음)."""
    client = request.client.host if request.client else "-"
    log.info(
        "host=%s client=%s method=%s path=%s status=%s bytes=%d ms=%d stream=%d",
        host_id,
        client,
        request.method,
        request.url.path,
        status,
        size,
        int((time.monotonic() - started) * 1000),
        stream.id,
    )


# ── 헬스 ────────────────────────────────────────────────────────────────
@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, Any]:
    """protocol.md §7 — 인증 없음, 본문 없음."""
    hosts, sessions, streams = hub.counts()
    return {"ok": True, "hosts": hosts, "sessions": sessions, "streams": streams}


# ── 에이전트 WS ─────────────────────────────────────────────────────────
@app.websocket("/agent")
async def agent_endpoint(websocket: WebSocket) -> None:
    await hub.serve(websocket)


# ── 프록시 ──────────────────────────────────────────────────────────────
@app.api_route("/h/{host_id}", methods=METHODS, include_in_schema=False)
@app.api_route("/h/{host_id}/{rest:path}", methods=METHODS, include_in_schema=False)
async def proxy_http(host_id: str, request: Request, rest: str = "") -> Response:
    """폰 요청을 해당 `host_id` 의 에이전트 세션으로 전달한다."""
    started = time.monotonic()
    if not is_valid_host_id(host_id):
        log.info("invalid host id path=%s", request.url.path)
        return JSONResponse({"detail": "invalid host id"}, status_code=404)
    session = hub.get(host_id)
    if session is None:
        return JSONResponse({"detail": "host offline"}, status_code=503)
    try:
        stream = session.open_stream()
    except TooManyStreams:
        log.info("too many streams host=%s", host_id)
        return JSONResponse({"detail": "too many streams"}, status_code=429)

    path = f"/{rest}" if rest else "/"
    head_payload = {
        "method": request.method,
        "path": path,
        "query": request.url.query,
        "headers": _request_headers(request),
        "has_body": _declares_body(request),
    }
    try:
        await session.send_json(mux.TYPE_REQ_HEAD, stream.id, head_payload)
    except Exception as exc:
        session.close_stream(stream.id)
        log.warning("REQ_HEAD 전송 실패 host=%s: %r", host_id, exc)
        return JSONResponse({"detail": "relay error"}, status_code=502)

    failure = await _forward_request_body(request, session, stream)
    if failure is not None:
        return failure

    # 응답 헤더 대기(30초) / 무진행(60초) 은 세션 워치독이 CANCEL 로 종료시킨다.
    while True:
        item = await _await_item(request, session, stream)
        if item is None:
            session.close_stream(stream.id)
            return JSONResponse({"detail": "client disconnect"}, status_code=499)
        kind = item[0]
        if kind == "head":
            break
        session.close_stream(stream.id)
        if kind == "end":
            log.info("agent closed stream host=%s stream=%d", host_id, stream.id)
            return JSONResponse({"detail": "agent closed stream"}, status_code=502)
        status = mux.status_for_reason(stream.cancel_reason)
        log.info(
            "stream cancelled host=%s stream=%d reason=%s status=%d",
            host_id,
            stream.id,
            stream.cancel_reason,
            status,
        )
        return JSONResponse({"detail": mux.detail_for_reason(stream.cancel_reason)}, status_code=status)

    assert stream.status is not None
    headers = _response_headers(stream)
    if stream.status in (204, 304):
        session.close_stream(stream.id)
        return Response(status_code=stream.status, headers=headers)
    return StreamingResponse(
        _iter_response(host_id, request, session, stream, started),
        status_code=stream.status,
        headers=headers,
    )


async def _forward_request_body(request: Request, session: AgentSession, stream: Stream) -> Response | None:
    """요청 본문을 REQ_BODY 로 흘려보낸다. 실패 시(413/499) 응답을 반환한다."""
    declared = _content_length(request)
    if declared is not None and declared > mux.MAX_BODY:
        await session.cancel_stream(stream.id, "too_large")
        session.close_stream(stream.id)
        log.info("request body too large(선언) host=%s stream=%d bytes=%d", session.host_id, stream.id, declared)
        return JSONResponse({"detail": BODY_DETAIL}, status_code=413)

    total = 0
    if _declares_body(request):
        try:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                stream.touch()
                if total > mux.MAX_BODY:
                    await session.cancel_stream(stream.id, "too_large")
                    session.close_stream(stream.id)
                    log.info(
                        "request body too large host=%s stream=%d bytes=%d",
                        session.host_id,
                        stream.id,
                        total,
                    )
                    return JSONResponse({"detail": BODY_DETAIL}, status_code=413)
                for offset in range(0, len(chunk), CHUNK_SIZE):
                    await session.send_frame(
                        mux.TYPE_REQ_BODY, stream.id, chunk[offset : offset + CHUNK_SIZE]
                    )
        except ClientDisconnect:
            session.cancel_soon(stream.id, "client_gone")
            return JSONResponse({"detail": "client disconnect"}, status_code=499)
        except Exception as exc:
            session.close_stream(stream.id)
            log.warning("REQ_BODY 전송 실패 host=%s: %r", session.host_id, exc)
            return JSONResponse({"detail": "relay error"}, status_code=502)

    try:
        await session.send_frame(mux.TYPE_REQ_END, stream.id)
    except Exception as exc:
        session.close_stream(stream.id)
        log.warning("REQ_END 전송 실패 host=%s: %r", session.host_id, exc)
        return JSONResponse({"detail": "relay error"}, status_code=502)
    stream.request_ended_at = time.monotonic()
    stream.touch()
    return None


async def _await_item(
    request: Request, session: AgentSession, stream: Stream
) -> tuple[str, Any] | None:
    """헤더/종료/취소 이벤트를 기다린다. 클라이언트가 끊기면 None."""
    while True:
        try:
            return await asyncio.wait_for(stream.queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            if await _client_gone(request):
                session.cancel_soon(stream.id, "client_gone")
                return None


async def _client_gone(request: Request) -> bool:
    try:
        return await request.is_disconnected()
    except Exception:
        return False


async def _iter_response(
    host_id: str, request: Request, session: AgentSession, stream: Stream, started: float
) -> AsyncIterator[bytes]:
    """RES_BODY 를 클라이언트로 흘려보낸다."""
    size = 0
    finished = False
    try:
        while True:
            kind, data = await stream.queue.get()
            if kind == "body":
                size += len(data)
                yield data
            elif kind == "end":
                finished = True
                break
            else:  # cancel — 상태는 이미 전송됐다(본문만 끊는다).
                break
    finally:
        if not finished:
            session.cancel_soon(stream.id, "client_gone")
        session.close_stream(stream.id)
        _log_line(host_id, request, stream, stream.status or 0, size, started)
