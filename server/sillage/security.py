"""Mots de passe, jetons de session et limitation des tentatives."""
from __future__ import annotations

import time
from collections import defaultdict, deque

import bcrypt
import jwt

COOKIE_NAME = "sillage_session"
TOKEN_DAYS = 30
MIN_PASSWORD = 10


def hash_password(password: str) -> str:
    # bcrypt ne lit que 72 octets : on tronque explicitement pour éviter une erreur.
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt(rounds=12)).decode()


def check_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], hashed.encode())
    except ValueError:
        return False


# Hachage factice pour garder un temps de réponse constant quand le compte n'existe pas.
DUMMY_HASH = bcrypt.hashpw(b"sillage-dummy-password", bcrypt.gensalt(rounds=12)).decode()


def make_token(secret: str, user_id: int, token_version: int) -> str:
    now = int(time.time())
    payload = {"sub": str(user_id), "tv": token_version, "iat": now, "exp": now + TOKEN_DAYS * 86400}
    return jwt.encode(payload, secret, algorithm="HS256")


def read_token(secret: str, token: str) -> tuple[int, int] | None:
    try:
        data = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["sub", "exp"]})
        return int(data["sub"]), int(data.get("tv", 0))
    except (jwt.PyJWTError, ValueError, KeyError):
        return None


class RateLimiter:
    """Au plus `limit` tentatives par fenêtre glissante de `window` secondes, par clé."""

    def __init__(self, limit: int = 10, window: float = 600.0):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        if len(self._hits) > 10_000:  # borne mémoire
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
                del self._hits[k]
        return True
