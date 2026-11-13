"""Score de copiabilité (0-100) et convergence par ticker."""
from __future__ import annotations

import re
from datetime import date

_TOP_TITLE = re.compile(
    r"\b(CEO|CFO|COO|President|Chair(man|woman|person)?|Founder|Co-?Founder|"
    r"Chief\s+Executive|Chief\s+Financial|Chief\s+Operating)\b",
    re.IGNORECASE,
)


def _clamp(x: float) -> int:
    return int(max(0, min(100, round(x))))


def _money(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f} M$".replace(".", ",")
    if v >= 1_000:
        return f"{v / 1_000:.0f} K$"
    return f"{v:.0f} $"


def days_between(a: date | None, b: date | None) -> int | None:
    if a is None or b is None:
        return None
    return (b - a).days


def insider_score(
    *,
    code: str,
    officer_title: str | None,
    is_officer: bool,
    is_director: bool,
    is_ten_percent: bool,
    value: float | None,
    delay_days: int | None,
    cluster: int,
    plan_10b5_1: bool,
    price_change: float | None,
) -> tuple[int, list[str]]:
    reasons: list[str] = []
    base = {"P": 40, "S": 12, "M": 5}.get(code, 0)
    label = {"P": "Achat sur le marché", "S": "Vente", "M": "Levée d'options"}.get(code, code)
    reasons.append(f"{label} (+{base})")
    score = base

    if officer_title and _TOP_TITLE.search(officer_title):
        score += 15
        reasons.append(f"Dirigeant de premier plan : {officer_title} (+15)")
    elif is_officer or is_director:
        score += 8
        reasons.append(("Dirigeant" if is_officer else "Administrateur") + " (+8)")
    elif is_ten_percent:
        score += 5
        reasons.append("Actionnaire de plus de 10 % (+5)")

    v = float(value or 0)
    if v >= 1_000_000:
        score += 15
        reasons.append(f"Montant élevé : {_money(v)} (+15)")
    elif v >= 250_000:
        score += 10
        reasons.append(f"Montant significatif : {_money(v)} (+10)")
    elif v >= 50_000:
        score += 5
        reasons.append(f"Montant notable : {_money(v)} (+5)")

    if code == "P" and delay_days is not None:
        if delay_days <= 2:
            score += 10
            reasons.append(f"Déclaré vite : {delay_days} j (+10)")
        elif delay_days <= 5:
            score += 5
            reasons.append(f"Déclaré en {delay_days} j (+5)")

    if cluster >= 3:
        score += 15
        reasons.append(f"Cluster : {cluster} initiés acheteurs sur 30 j (+15)")
    elif cluster == 2:
        score += 8
        reasons.append("Cluster : 2 initiés acheteurs sur 30 j (+8)")

    if plan_10b5_1:
        score -= 20
        reasons.append("Plan 10b5-1 programmé à l'avance (−20)")

    if price_change is not None and price_change > 0.10:
        score -= 10
        reasons.append(f"Cours déjà monté de {price_change * 100:.0f} % depuis le trade (−10)".replace(".", ","))

    return _clamp(score), reasons


def congress_score(
    *,
    side: str,
    amount_min: float | None,
    delay_days: int | None,
    cluster: int,
) -> tuple[int, list[str]]:
    reasons: list[str] = []
    if side == "buy":
        score = 35
        reasons.append("Achat d'un élu (+35)")
    else:
        score = 15
        reasons.append("Vente d'un élu (+15)")

    a = float(amount_min or 0)
    if a >= 250_000:
        score += 15
        reasons.append(f"Montant minimum {_money(a)} (+15)")
    elif a >= 50_000:
        score += 10
        reasons.append(f"Montant minimum {_money(a)} (+10)")
    elif a >= 15_000:
        score += 5
        reasons.append(f"Montant minimum {_money(a)} (+5)")

    if delay_days is not None:
        if delay_days <= 15:
            score += 15
            reasons.append(f"Déclaré en {delay_days} j (+15)")
        elif delay_days <= 30:
            score += 5
            reasons.append(f"Déclaré en {delay_days} j (+5)")
        else:
            score -= 10
            reasons.append(f"Déclaré tard : {delay_days} j (−10)")

    if cluster >= 3:
        score += 15
        reasons.append(f"Cluster : {cluster} élus sur 30 j (+15)")
    elif cluster == 2:
        score += 8
        reasons.append("Cluster : 2 élus sur 30 j (+8)")

    return _clamp(score), reasons


def convergence_score(*, insiders: int, congress: int, ownership: int) -> int:
    sources = sum(1 for n in (insiders, congress, ownership) if n > 0)
    raw = 20 * sources + 6 * min(insiders, 4) + 6 * min(congress, 4) + 10 * min(ownership, 2)
    return min(100, raw)
