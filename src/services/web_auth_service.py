"""Opaque, revocable web sessions; credentials and tokens never enter browser storage."""

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from src.infrastructure.config.settings import get_settings


SESSION_COOKIE = "goofish_session"


@dataclass(frozen=True)
class WebSession:
    username: str
    expires_at: float
    credential_version: bytes


class WebAuthService:
    def __init__(self, *, settings_provider=get_settings, clock=time.time):
        self.settings_provider = settings_provider
        self.clock = clock
        self._sessions: dict[str, WebSession] = {}

    def _credential_version(self) -> bytes:
        settings = self.settings_provider()
        return hashlib.sha256(f"{settings.web_username}\0{settings.web_password}".encode()).digest()

    def login(self, username: str, password: str) -> str | None:
        settings = self.settings_provider()
        valid_user = hmac.compare_digest(username.encode(), settings.web_username.encode())
        valid_password = hmac.compare_digest(password.encode(), settings.web_password.encode())
        if not (valid_user and valid_password):
            return None
        now = self.clock()
        self._sessions = {key: value for key, value in self._sessions.items() if value.expires_at > now}
        if len(self._sessions) >= 512:
            self._sessions.pop(next(iter(self._sessions)))
        token = secrets.token_urlsafe(32)
        self._sessions[self._token_key(token)] = WebSession(
            username=username,
            expires_at=now + settings.web_session_ttl_seconds,
            credential_version=self._credential_version(),
        )
        return token

    @staticmethod
    def _token_key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def validate(self, token: str | None) -> WebSession | None:
        if not token:
            return None
        key = self._token_key(token)
        session = self._sessions.get(key)
        if session is None:
            return None
        if session.expires_at <= self.clock() or not hmac.compare_digest(
            session.credential_version, self._credential_version()
        ):
            self._sessions.pop(key, None)
            return None
        return session

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(self._token_key(token), None)
