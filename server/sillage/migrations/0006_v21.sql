-- v2.1 : comptes désactivables, historique des tâches, secteurs, entraînement avancé, alertes de cours

ALTER TABLE users ADD COLUMN IF NOT EXISTS disabled BOOLEAN NOT NULL DEFAULT false;

-- Historique des exécutions de tâches (page Santé), 7 jours gardés
CREATE TABLE IF NOT EXISTS job_runs (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_s  NUMERIC,
    ok          BOOLEAN NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS job_runs_name ON job_runs (name, finished_at DESC);

-- Secteur d'activité des sociétés (code SIC de la SEC)
CREATE TABLE IF NOT EXISTS company_info (
    cik        BIGINT PRIMARY KEY,
    name       TEXT,
    sic        TEXT,
    sic_label  TEXT,
    sector     TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Entraînement : stop suiveur, objectif de gain, note
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS trailing_pct NUMERIC;
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS high_price NUMERIC;
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS target_price NUMERIC;
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS note TEXT;
ALTER TABLE paper_orders DROP CONSTRAINT IF EXISTS paper_orders_reason_check;
ALTER TABLE paper_orders ADD CONSTRAINT paper_orders_reason_check
    CHECK (reason IN ('manual', 'stop', 'rule', 'horizon', 'target', 'trailing'));

-- Alertes de cours
CREATE TABLE IF NOT EXISTS price_alerts (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ticker       TEXT NOT NULL,
    direction    TEXT NOT NULL CHECK (direction IN ('above', 'below')),
    price        NUMERIC NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    triggered_at TIMESTAMPTZ,
    triggered_price NUMERIC,
    notified     BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS price_alerts_active ON price_alerts (ticker) WHERE triggered_at IS NULL;

-- Historique AMF : pagination progressive vers le passé
CREATE TABLE IF NOT EXISTS amf_history_state (id INTEGER PRIMARY KEY DEFAULT 1, next_offset INTEGER NOT NULL DEFAULT 0,
                                              done BOOLEAN NOT NULL DEFAULT false);
