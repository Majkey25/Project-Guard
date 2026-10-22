from __future__ import annotations

import os
from collections import deque
from ipaddress import ip_address
from secrets import compare_digest
from threading import BoundedSemaphore, Lock
from time import monotonic

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 128 * 1024
MAX_CONCURRENT_REQUESTS = 4
MAX_REQUESTS_PER_MINUTE = 60


def loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def api_token() -> str:
    return os.environ.get("PROJECT_GUARD_API_TOKEN", "").strip()


class ApiAccess:
    """One operator per process; protect credentials before parsing requests or doing work."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._slots = BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        self._lock = Lock()
        self._requests: deque[float] = deque()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        token = api_token()
        if token:
            authenticated = compare_digest(
                request.headers.get("authorization", "").encode(), f"Bearer {token}".encode()
            )
        else:
            # Peer and Host checks also cover direct ASGI launches and DNS rebinding.
            authenticated = (
                request.client is not None
                and loopback_host(request.client.host)
                and loopback_host(request.url.hostname or "")
                and request.headers.get("sec-fetch-site", "none") in {"none", "same-origin"}
                and request.headers.get("origin", str(request.base_url).rstrip("/"))
                == str(request.base_url).rstrip("/")
            )
        if not authenticated:
            await JSONResponse(
                {"detail": "Authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            await JSONResponse({"detail": "Invalid request size"}, status_code=413)(
                scope, receive, send
            )
            return
        if scope["path"] == "/status":
            await self.app(scope, receive, send)
            return
        with self._lock:
            now = monotonic()
            while self._requests and self._requests[0] <= now - 60:
                self._requests.popleft()
            admitted = len(self._requests) < MAX_REQUESTS_PER_MINUTE
            if admitted:
                self._requests.append(now)
        if not admitted or not self._slots.acquire(blocking=False):
            await JSONResponse({"detail": "API busy; retry later"}, status_code=429)(
                scope, receive, send
            )
            return
        consumed = 0

        async def bounded_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > MAX_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="Request body too large")
            return message

        try:
            # Keep the slot until an SSE stream finishes or its client disconnects.
            await self.app(scope, bounded_receive, send)
        finally:
            self._slots.release()
