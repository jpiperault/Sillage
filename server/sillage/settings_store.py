"""Réglages modifiables par l'administrateur dans l'interface (table app_settings).

Ordre de priorité : valeur enregistrée dans l'interface > variable d'environnement > défaut.
Restent uniquement en variables : DATABASE_URL, JWT_SECRET, ADMIN_EMAIL, ADMIN_PASSWORD (nécessaires au démarrage).
"""
from __future__ import annotations

import dataclasses
import re

from . import log
from .config import Settings
from .ingest.funds import DEFAULT_FUNDS, parse_funds_setting

_log = log.get("settings")

# clé : (type, libellé, secret, aide, section)
EDITABLE: dict[str, tuple[str, str, bool, str, str]] = {
    "sec_user_agent": ("str", "Contact envoyé à la SEC et au Greffe", False,
                       "En-tête User-Agent, ex. « Sillage/1.0 sillage@domaine.fr ». Vide = Sillage/1.0 + ADMIN_EMAIL. "
                       "La SEC exige une adresse joignable.", "Collecte"),
    "ingest_interval_min": ("int", "Intervalle de collecte (minutes)", False, "Entre deux cycles, 10 par défaut.", "Collecte"),
    "backfill_days": ("int", "Rattrapage Form 4 (jours ouvrés)", False, "Index quotidiens SEC relus, 3 par défaut.", "Collecte"),
    "form144_enabled": ("bool", "Form 144 (ventes annoncées)", False, "", "Sources"),
    "house_enabled": ("bool", "Élus : Greffe de la Chambre", False, "", "Sources"),
    "quiver_api_key": ("str", "Clé Quiver Quantitative", True, "Optionnelle, payante : ajoute le Sénat.", "Sources"),
    "amf_enabled": ("bool", "France : déclarations AMF", False, "", "Sources"),
    "finra_enabled": ("bool", "Ventes à découvert FINRA", False, "", "Sources"),
    "bafin_enabled": ("bool", "Allemagne : déclarations BaFin", False, "", "Sources"),
    "senate_enabled": ("bool", "Élus : Sénat (eFD)", False,
                       "Non testé : le site du Sénat bloque certains serveurs. Utilisez « Tester l'accès au Sénat ».",
                       "Sources"),
    "funds_13f": ("str", "Fonds suivis (13F)", False,
                  "Vide = 18 grands fonds par défaut ; sinon CIK séparés par des virgules ; « off » pour désactiver.",
                  "Sources"),
    "backtest_enabled": ("bool", "Backtest du score", False, "Historique SEC + cours Nasdaq, recalculé chaque jour.",
                         "Backtest"),
    "backtest_quarters": ("int", "Trimestres d'historique", False, "6 par défaut (~1 an et demi).", "Backtest"),
    "score_version": ("choice:auto,v1,v2", "Score affiché", False,
                      "auto = score v2 (calibré sur le backtest) s'il est validé, sinon v1 (points fixes).", "Backtest"),
    "telegram_bot_token": ("str", "Jeton du bot Telegram", True,
                           "Créé avec @BotFather dans Telegram. Active les alertes.", "Alertes"),
    "public_url": ("str", "Adresse publique du site", False, "Pour les liens des alertes, ex. https://sillage.piperault.fr",
                   "Alertes"),
    "allow_signup": ("bool", "Inscriptions ouvertes", False, "", "Comptes"),
    "paper_start_cash": ("float", "Capital fictif de départ ($)", False,
                         "Pour les nouveaux comptes et les réinitialisations.", "Comptes"),
}


class SettingError(ValueError):
    pass


def convert(key: str, raw: str | None):
    """Texte saisi → valeur typée du réglage ; lève SettingError si invalide."""
    kind = EDITABLE[key][0]
    v = (raw or "").strip()
    if kind.startswith("choice:"):
        choices = kind.split(":", 1)[1].split(",")
        if v.lower() not in choices:
            raise SettingError("valeur attendue : " + ", ".join(choices))
        return v.lower()
    if kind == "bool":
        if v.lower() in ("1", "true", "oui", "on", "yes"):
            return True
        if v.lower() in ("0", "false", "non", "off", "no", ""):
            return False
        raise SettingError("valeur attendue : oui ou non")
    if kind == "int":
        if not re.fullmatch(r"\d{1,6}", v):
            raise SettingError("nombre entier attendu")
        n = int(v)
        if key == "ingest_interval_min" and n < 1:
            raise SettingError("au moins 1 minute")
        if key == "backtest_quarters" and not 1 <= n <= 12:
            raise SettingError("entre 1 et 12")
        return n
    if kind == "float":
        try:
            f = float(v.replace(",", ".").replace(" ", ""))
        except ValueError as exc:
            raise SettingError("nombre attendu") from exc
        if f <= 0:
            raise SettingError("doit être positif")
        return f
    if key == "public_url" and v and not re.match(r"^https?://", v):
        raise SettingError("doit commencer par https://")
    if key == "sec_user_agent" and v and "@" not in v:
        raise SettingError("doit contenir une adresse e-mail")
    return v.rstrip("/") if key == "public_url" else v


def apply(base: Settings, overrides: dict[str, str | None]) -> Settings:
    changes = {}
    for key, raw in overrides.items():
        if key not in EDITABLE or raw is None:
            continue
        try:
            value = convert(key, raw)
        except SettingError as exc:
            _log.warning("réglage %s ignoré : %s", key, exc)
            continue
        if key == "funds_13f":
            value = parse_funds_setting(value)
        elif key == "sec_user_agent" and not value:
            value = f"Sillage/1.0 {base.admin_email}"
        elif key in ("quiver_api_key", "telegram_bot_token"):
            value = value or None
        changes[key] = value
    return dataclasses.replace(base, **changes)


async def load(conn) -> dict[str, str | None]:
    cur = await conn.execute("SELECT key, value FROM app_settings")
    rows = await cur.fetchall()
    await conn.commit()
    return {r["key"]: r["value"] for r in rows}


def describe(base: Settings, overrides: dict[str, str | None], effective: Settings) -> list[dict]:
    out = []
    for key, (kind, label, secret, help_, section) in EDITABLE.items():
        value = getattr(effective, key)
        if key == "funds_13f":
            value = ", ".join(str(c) for c in value) if value else "off"
            if set(effective.funds_13f) == set(DEFAULT_FUNDS):
                value = ""
        shown = ("••••" + str(value)[-4:] if value else "") if secret else value
        out.append({"key": key, "type": kind, "label": label, "help": help_, "section": section, "secret": secret,
                    "value": shown, "is_set": bool(value),
                    "source": "interface" if overrides.get(key) is not None else "variable ou défaut"})
    return out
