"""Authentication for HTTP APIs, static screenshots and WebSocket handshakes."""

from urllib.parse import urlsplit

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.datastructures import MutableHeaders

from src.services.web_auth_service import SESSION_COOKIE, WebAuthService


def same_origin(connection: HTTPConnection, sessions: WebAuthService) -> bool:
    origin = connection.headers.get("origin")
    if not origin:  # Non-browser clients still have to present a valid session.
        return True
    scheme = {"ws": "http", "wss": "https"}.get(connection.url.scheme, connection.url.scheme)
    expected = f"{scheme}://{connection.url.netloc}"
    allowed = sessions.settings_provider().web_allowed_origins
    origins = [expected, *[value.strip().rstrip("/") for value in allowed.split(",") if value.strip()]]
    try:
        parsed = urlsplit(origin)
        return parsed.scheme in {"http", "https"} and origin.rstrip("/") in origins
    except ValueError:
        return False


class WebAuthMiddleware:
    def __init__(self, app, *, sessions: WebAuthService):
        self.app = app
        self.sessions = sessions

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        connection = HTTPConnection(scope)
        path = scope["path"]
        root_path = scope.get("root_path", "").rstrip("/")
        if root_path and (path == root_path or path.startswith(root_path + "/")):
            path = path[len(root_path):]
        path = path.rstrip("/")
        protected = (
            path == "/api" or path.startswith("/api/")
            or path == "/ws" or path.startswith("/ws/")
            or path == "/static" or path.startswith("/static/")
            or path in {"/docs", "/redoc", "/openapi.json"}
        )
        if protected and self.sessions.validate(connection.cookies.get(SESSION_COOKIE)) is None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4401})
            else:
                await JSONResponse({"detail": "请先登录"}, status_code=401, headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        requires_origin_check = scope["type"] == "websocket" or (
            (protected or path.startswith("/auth/"))
            and scope.get("method") not in {"GET", "HEAD", "OPTIONS"}
        )
        if requires_origin_check and not same_origin(connection, self.sessions):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4403})
            else:
                await JSONResponse({"detail": "不允许跨站请求"}, status_code=403, headers={"Cache-Control": "no-store"})(scope, receive, send)
            return

        async def send_private(message):
            if scope["type"] == "http" and message["type"] == "http.response.start" and (protected or path.startswith("/auth/")):
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_private)
