"""Thin aiohttp-based stand-in for the BGOS backend.

Used by the pytest suite to exercise BgosApi / BgosWs without needing a real
NestJS backend running. Keeps the route DSL minimal — `.on(method, path).respond(...)`
registers a canned response; every request is captured in `.requests` for assertions.

Socket.IO support (Task 3): when started, a `socketio.AsyncServer` is attached to
the same aiohttp app. Client connections are tracked in `_SocketConnection`
records with their query string + rooms joined. The test can emit server→client
events via `emit_to_room` and force-disconnect specific connections via
`force_disconnect_last_socket`.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import socketio
from aiohttp import web


@dataclass
class RecordedRequest:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes

    @property
    def json_body(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except json.JSONDecodeError:
            return None


@dataclass
class _Response:
    status: int = 200
    json_body: Any = None
    text_body: str | None = None
    bytes_body: bytes | None = None
    headers: dict[str, str] | None = None


class _RouteBuilder:
    def __init__(self, server: "MockBgosServer", method: str, path: str) -> None:
        self._server = server
        self._method = method.upper()
        self._path = path

    def respond(
        self,
        status: int = 200,
        json_body: Any = None,
        *,
        text: str | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> "_RouteBuilder":
        self._server._routes[(self._method, self._path)] = _Response(
            status=status, json_body=json_body, text_body=text, bytes_body=data,
            headers=headers,
        )
        return self


@dataclass
class _SocketConnection:
    sid: str
    query: dict[str, str]
    rooms_joined: set[str] = field(default_factory=set)
    disconnected: bool = False


class MockBgosServer:
    """Minimal aiohttp + Socket.IO stand-in for the BGOS backend.

    HTTP usage:
        server = MockBgosServer()
        await server.start()
        server.on("GET", "/api/v1/integrations/me").respond(200, {"pairing_id": 42})
        # ... run client ...
        req = server.last_request("GET", "/api/v1/integrations/me")
        assert req.headers["X-BGOS-Pairing"] == "pair_xyz"
        await server.stop()

    Socket.IO usage:
        # After a client connects via `ws.start()`:
        await server.wait_for_socket_connection()
        conn = server.last_socket_connection()
        assert conn.query["pairingToken"] == "pair_xyz"
        # Emit a server→client event (client must have joined the room):
        await server.emit_to_room("assistant:7", "inbound_message", {...})
        # Force disconnect to exercise reconnect logic:
        await server.force_disconnect_last_socket()
    """

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], _Response] = {}
        self.requests: list[RecordedRequest] = []
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._port: int | None = None
        # Socket.IO state
        self._sio: socketio.AsyncServer | None = None
        self._socket_connections: list[_SocketConnection] = []
        self._connection_event = asyncio.Event()

    @property
    def url(self) -> str:
        if self._port is None:
            raise RuntimeError("mock server not started")
        return f"http://127.0.0.1:{self._port}"

    def on(self, method: str, path: str) -> _RouteBuilder:
        return _RouteBuilder(self, method, path)

    def last_request(self, method: str, path: str) -> RecordedRequest:
        method_u = method.upper()
        for req in reversed(self.requests):
            if req.method == method_u and req.path == path:
                return req
        raise AssertionError(f"no recorded request for {method_u} {path}")

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        recorded = RecordedRequest(
            method=request.method,
            path=request.path,
            query=dict(request.rel_url.query),
            headers=dict(request.headers),
            body=body,
        )
        self.requests.append(recorded)

        route = self._routes.get((request.method, request.path))
        if route is None:
            return web.json_response(
                {"error": "no_mock_route", "method": request.method, "path": request.path},
                status=501,
            )
        extra_headers = route.headers or None
        if route.json_body is not None:
            return web.json_response(
                route.json_body, status=route.status, headers=extra_headers,
            )
        if route.text_body is not None:
            return web.Response(
                text=route.text_body, status=route.status, headers=extra_headers,
            )
        if route.bytes_body is not None:
            return web.Response(
                body=route.bytes_body, status=route.status, headers=extra_headers,
            )
        return web.Response(status=route.status, headers=extra_headers)

    # -------------------------------------------------------------------------
    # Socket.IO helpers
    # -------------------------------------------------------------------------

    def _find_conn(self, sid: str) -> _SocketConnection | None:
        for conn in self._socket_connections:
            if conn.sid == sid:
                return conn
        return None

    async def wait_for_socket_connection(
        self, count: int = 1, timeout: float = 3.0,
    ) -> None:
        """Wait until at least `count` distinct client connections have arrived.

        Distinct connections include reconnects — each re-handshake produces a
        new entry in _socket_connections. Polls with a short sleep to avoid
        the event-based race where multiple rapid connects could skip a waiter.
        """
        async def _wait() -> None:
            while len(self._socket_connections) < count:
                await asyncio.sleep(0.05)
        await asyncio.wait_for(_wait(), timeout=timeout)

    def last_socket_connection(self) -> _SocketConnection:
        if not self._socket_connections:
            raise AssertionError("no Socket.IO client has connected yet")
        return self._socket_connections[-1]

    async def emit_to_room(self, room: str, event: str, data: Any) -> None:
        assert self._sio is not None, "server not started"
        await self._sio.emit(event, data, room=room)

    async def force_disconnect_last_socket(self) -> None:
        assert self._sio is not None, "server not started"
        conn = self.last_socket_connection()
        conn.disconnected = True
        await self._sio.disconnect(conn.sid)

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def start(self) -> None:
        app = web.Application()

        # Socket.IO server attached at /socket.io/ (the default path)
        self._sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*")

        @self._sio.event  # type: ignore[misc]
        async def connect(sid: str, environ: dict[str, Any]) -> None:
            qs = environ.get("QUERY_STRING", "")
            query = {k: v[0] for k, v in parse_qs(qs).items()}
            self._socket_connections.append(_SocketConnection(sid=sid, query=query))
            self._connection_event.set()

        @self._sio.event  # type: ignore[misc]
        async def join(sid: str, data: dict[str, Any]) -> None:
            room = data.get("room")
            if not room:
                return
            conn = self._find_conn(sid)
            if conn is not None:
                conn.rooms_joined.add(room)
            await self._sio.enter_room(sid, room)

        self._sio.attach(app)

        # HTTP catch-all routes come AFTER socket.io so /socket.io/* isn't swallowed
        app.router.add_route("*", "/{path:.*}", self._handle)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        assert self._site._server is not None and self._site._server.sockets
        self._port = self._site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        # socketio server lifecycle is tied to the aiohttp app runner
