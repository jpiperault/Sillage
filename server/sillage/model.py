"""Score v2 : probabilité qu'un achat d'initié batte sa référence de marché à 3 mois.

Régression logistique (Python pur, pas de dépendance) entraînée sur le backtest :
- cible : écart de performance à 63 séances > 0 (référence SPY ou IWM selon la taille) ;
- variables : rôle, montant, délai, cluster, plan 10b5-1, taille vs position, prix, capitalisation, rachat
  d'actions récent, résultats imminents, track record de l'initié (calculé sans regarder le futur) ;
- validation chronologique : entraînement sur les 70 % les plus anciens, mesure sur les 30 % récents
  (AUC, part de gagnants du meilleur quintile) ; le modèle n'est activé que s'il fait mieux que le hasard ;
- score affiché = rang centile de la probabilité parmi les signaux d'entraînement (0-100).
"""
from __future__ import annotations

import bisect
import math
import random

from . import backtest

MIN_AUC = 0.53
LABELS = {
    "role=Dirigeant de premier plan": "Dirigeant de premier plan (CEO, CFO…)",
    "role=Autre dirigeant ou administrateur": "Dirigeant ou administrateur",
    "role=Actionnaire > 10 %": "Actionnaire > 10 %",
    "plan": "Plan 10b5-1",
    "cluster=2": "Cluster de 2 initiés",
    "cluster=3+": "Cluster de 3 initiés ou plus",
    "buyback": "Rachat d'actions annoncé récemment",
    "earnings_soon": "Résultats dans les 30 jours",
    "price<5": "Action à moins de 5 $",
    "track=good": "Initié souvent gagnant par le passé",
    "track=bad": "Initié souvent perdant par le passé",
}


def features(*, role: str, value: float, delay: int | None, cluster: int, plan: bool, position_pct: float | None,
             first_line: bool, price: float | None, market_cap: float | None, buyback: bool, earnings_soon: bool,
             track_wins: float | None) -> list[str]:
    f = [f"role={role}"]
    f.append("value=" + backtest._bucket(value, [(50_000, "<50K"), (250_000, "50-250K"), (1_000_000, "250K-1M")], ">=1M"))
    if delay is not None:
        f.append("delay=" + ("<=2" if delay <= 2 else "3-5" if delay <= 5 else ">5"))
    if cluster >= 3:
        f.append("cluster=3+")
    elif cluster == 2:
        f.append("cluster=2")
    if plan:
        f.append("plan")
    if first_line:
        f.append("pos=first")
    elif position_pct is not None:
        f.append("pos=" + backtest._bucket(position_pct, [(10, "<10"), (50, "10-50"), (100, "50-100")], ">=100"))
    if price is not None and price < 5:
        f.append("price<5")
    f.append("cap=" + backtest.cap_bucket(market_cap))
    if buyback:
        f.append("buyback")
    if earnings_soon:
        f.append("earnings_soon")
    if track_wins is not None:
        f.append("track=good" if track_wins >= 0.6 else "track=bad" if track_wins <= 0.4 else "track=mid")
    return f


def row_features(r: dict) -> list[str]:
    dims = backtest.dimensions(r, 0)
    shares = float(r["shares"] or 0)
    after = float(r["shares_after"]) if r["shares_after"] is not None else None
    pos = backtest.position_change("P", shares, after)
    return features(
        role=dims["Rôle"], value=float(r["value"] or 0), delay=(r["filed_date"] - r["trade_date"]).days,
        cluster=int(r["cluster"] or 0), plan=bool(r["plan_10b5_1"]), position_pct=pos,
        first_line=pos is None and after is not None, price=float(r["entry"]),
        market_cap=float(r["market_cap"]) if r.get("market_cap") else None, buyback=bool(r.get("buyback")),
        earnings_soon=bool(r.get("earnings_soon")), track_wins=r.get("track_wins"))


def _sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def train(samples: list[tuple[list[str], int]], l2: float = 1.0, epochs: int = 300, lr: float = 0.5) -> dict:
    """Descente de gradient par lots sur variables binaires. Renvoie {bias, weights}."""
    names = sorted({f for fs, _ in samples for f in fs})
    idx = {n: i for i, n in enumerate(names)}
    rows = [([idx[f] for f in fs], y) for fs, y in samples]
    n = len(rows)
    w = [0.0] * len(names)
    base = sum(y for _, y in rows) / n
    b = math.log(base / (1 - base)) if 0 < base < 1 else 0.0
    for _ in range(epochs):
        gw = [0.0] * len(names)
        gb = 0.0
        for fs, y in rows:
            err = _sigmoid(b + sum(w[i] for i in fs)) - y
            gb += err
            for i in fs:
                gw[i] += err
        b -= lr * gb / n
        for i in range(len(w)):
            w[i] -= lr * (gw[i] / n + l2 * w[i] / n)
    return {"bias": b, "weights": {names[i]: round(w[i], 5) for i in range(len(names))}}


def predict(model: dict, fs: list[str]) -> float:
    return _sigmoid(model["bias"] + sum(model["weights"].get(f, 0.0) for f in fs))


def auc(scores: list[float], ys: list[int]) -> float | None:
    pos = sum(ys)
    neg = len(ys) - pos
    if not pos or not neg:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    rank_sum, i = 0.0, 0
    while i < len(order):  # rangs moyens en cas d'égalité
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for k in range(i, j + 1):
            if ys[order[k]]:
                rank_sum += (i + j) / 2 + 1
        i = j + 1
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def fit(rows: list[dict]) -> dict | None:
    """rows : résultats d'OUTCOMES_SQL annotés (annotate_track). Renvoie modèle + métriques, ou None."""
    data = []
    for r in rows:
        if r["code"] != "P":
            continue
        ex = backtest.excess_returns(r).get(63)
        if ex is None:
            continue
        data.append((r["filed_date"], row_features(r), 1 if ex > 0 else 0, ex))
    if len(data) < 500:
        return None
    data.sort(key=lambda d: d[0])
    cut = int(len(data) * 0.7)
    train_set, test_set = data[:cut], data[cut:]
    m = train([(f, y) for _, f, y, _ in train_set])
    p_test = [predict(m, f) for _, f, _, _ in test_set]
    y_test = [y for _, _, y, _ in test_set]
    test_auc = auc(p_test, y_test)
    ranked = sorted(zip(p_test, test_set), key=lambda x: x[0], reverse=True)
    q = max(1, len(ranked) // 5)
    top = [d for _, d in ranked[:q]]
    bottom = [d for _, d in ranked[-q:]]

    def stats(ds):
        return {"n": len(ds), "win": round(sum(d[2] for d in ds) / len(ds) * 100, 1),
                "median": round(sorted(d[3] for d in ds)[len(ds) // 2] * 100, 2)}

    final = train([(f, y) for _, f, y, _ in data])
    probs = sorted(predict(final, f) for _, f, _, _ in data)
    quantiles = [probs[min(len(probs) - 1, int(i / 100 * len(probs)))] for i in range(101)]
    metrics = {
        "samples": len(data), "train": len(train_set), "test": len(test_set),
        "train_period": [train_set[0][0].isoformat(), train_set[-1][0].isoformat()],
        "test_period": [test_set[0][0].isoformat(), test_set[-1][0].isoformat()],
        "auc": round(test_auc, 3) if test_auc is not None else None,
        "test_all": stats(test_set), "test_top20": stats(top), "test_bottom20": stats(bottom),
        "validated": bool(test_auc and test_auc >= MIN_AUC
                          and stats(top)["win"] > stats(test_set)["win"]),
    }
    final["quantiles"] = quantiles
    return {"model": final, "metrics": metrics}


def percentile(model: dict, p: float) -> int:
    return max(0, min(100, bisect.bisect_left(model["quantiles"], p)))


def explain(model: dict, fs: list[str], p: float, limit: int = 5) -> list[str]:
    """Les critères qui pèsent le plus, en points de probabilité (approximation linéaire)."""
    contrib = [(f, model["weights"].get(f, 0.0)) for f in fs]
    contrib = [(f, w) for f, w in contrib if abs(w) >= 0.02]
    contrib.sort(key=lambda x: abs(x[1]), reverse=True)
    out = []
    for f, w in contrib[:limit]:
        pts = w * p * (1 - p) * 100
        label = LABELS.get(f) or describe_feature(f)
        out.append(f"{label} ({'+' if pts >= 0 else '−'}{abs(pts):.1f} pt de probabilité)".replace(".", ","))
    return out


def describe_feature(f: str) -> str:
    k, _, v = f.partition("=")
    return {"value": f"Montant {v.replace('<', '< ').replace('>=', '≥ ')} $", "delay": f"Délai de déclaration {v} j",
            "pos": "Première ligne dans la société" if v == "first" else f"Ligne augmentée de {v} %",
            "cap": f"Capitalisation {v}", "role": v, "track": "Track record neutre"}.get(k, f)


def sample_check() -> float:  # utilisé par les tests : modèle trivial sur données synthétiques
    random.seed(1)
    samples = [(["a"], 1 if random.random() < 0.8 else 0) for _ in range(300)] + \
              [(["b"], 1 if random.random() < 0.2 else 0) for _ in range(300)]
    m = train(samples)
    return predict(m, ["a"]) - predict(m, ["b"])
