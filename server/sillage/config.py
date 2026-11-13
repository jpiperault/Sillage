"""Configuration lue dans les variables d'environnement."""
from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    """Variable obligatoire absente ou invalide."""


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "oui", "on")


def _int(env: dict, name: str, default: int, minimum: int = 0) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un nombre entier (reçu : {raw!r})") from exc
    if value < minimum:
        raise ConfigError(f"{name} doit être au moins {minimum}")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str
    admin_email: str
    admin_password: str | None
    sec_user_agent: str
    quiver_api_key: str | None
    allow_signup: bool
    ingest_enabled: bool
    ingest_interval_min: int
    backfill_days: int
    paper_start_cash: float
    port: int

    @property
    def congress_enabled(self) -> bool:
        return bool(self.quiver_api_key)


def load_settings(env: dict | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    missing = [n for n in ("DATABASE_URL", "JWT_SECRET", "ADMIN_EMAIL") if not (env.get(n) or "").strip()]
    if missing:
        raise ConfigError("variable(s) d'environnement obligatoire(s) manquante(s) : " + ", ".join(missing))

    jwt_secret = env["JWT_SECRET"].strip()
    if len(jwt_secret) < 32:
        raise ConfigError("JWT_SECRET doit faire au moins 32 caractères (openssl rand -hex 32)")

    admin_email = env["ADMIN_EMAIL"].strip().lower()
    if "@" not in admin_email:
        raise ConfigError("ADMIN_EMAIL doit être une adresse e-mail")

    admin_password = (env.get("ADMIN_PASSWORD") or "").strip() or None
    if admin_password is not None and len(admin_password) < 10:
        raise ConfigError("ADMIN_PASSWORD doit faire au moins 10 caractères")

    try:
        cash = float(env.get("PAPER_START_CASH") or 25000)
    except ValueError as exc:
        raise ConfigError("PAPER_START_CASH doit être un nombre") from exc
    if cash <= 0:
        raise ConfigError("PAPER_START_CASH doit être positif")

    return Settings(
        database_url=env["DATABASE_URL"].strip(),
        jwt_secret=jwt_secret,
        admin_email=admin_email,
        admin_password=admin_password,
        sec_user_agent=(env.get("SEC_USER_AGENT") or "").strip() or f"Sillage/1.0 {admin_email}",
        quiver_api_key=(env.get("QUIVER_API_KEY") or "").strip() or None,
        allow_signup=_bool(env.get("ALLOW_SIGNUP"), True),
        ingest_enabled=_bool(env.get("INGEST_ENABLED"), True),
        ingest_interval_min=_int(env, "INGEST_INTERVAL_MIN", 10, minimum=1),
        backfill_days=_int(env, "BACKFILL_DAYS", 3, minimum=0),
        paper_start_cash=cash,
        port=_int(env, "PORT", 8080, minimum=1),
    )
