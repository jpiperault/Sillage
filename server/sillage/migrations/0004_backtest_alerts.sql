-- Backtest du score, cours historiques, réglages modifiables dans l'interface, alertes Telegram

-- Cours de clôture quotidiens (Nasdaq), pour le backtest et la fiche action
CREATE TABLE IF NOT EXISTS price_history (
    ticker TEXT NOT NULL,
    day    DATE NOT NULL,
    close  NUMERIC NOT NULL,
    idx    INTEGER NOT NULL,          -- numéro de séance (0 = plus ancienne) : rendements à N séances
    PRIMARY KEY (ticker, day)
);
CREATE UNIQUE INDEX IF NOT EXISTS price_history_idx ON price_history (ticker, idx);

CREATE TABLE IF NOT EXISTS price_history_state (
    ticker     TEXT PRIMARY KEY,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    rows       INTEGER NOT NULL DEFAULT 0,
    ok         BOOLEAN NOT NULL DEFAULT true
);

-- Historique des Form 4 (jeux de données trimestriels de la SEC), une ligne par déclaration et par code
CREATE TABLE IF NOT EXISTS bt_signals (
    accession     TEXT NOT NULL,
    code          TEXT NOT NULL,
    quarter       TEXT NOT NULL,
    ticker        TEXT,
    issuer_cik    BIGINT,
    owner_cik     BIGINT,
    owner_name    TEXT,
    relationship  TEXT,
    officer_title TEXT,
    shares        NUMERIC,
    value         NUMERIC,
    price         NUMERIC,
    shares_after  NUMERIC,
    trade_date    DATE,
    filed_date    DATE,
    plan_10b5_1   BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (accession, code)
);
CREATE INDEX IF NOT EXISTS bt_signals_ticker ON bt_signals (ticker, trade_date);

CREATE TABLE IF NOT EXISTS bt_quarters (
    quarter     TEXT PRIMARY KEY,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    signals     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bt_reports (
    id         BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    report     JSONB NOT NULL
);

-- Réglages modifiables par l'administrateur (priment sur les variables d'environnement)
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by BIGINT
);

-- Alertes Telegram par compte
CREATE TABLE IF NOT EXISTS user_alerts (
    user_id     BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    chat_id     BIGINT,
    link_code   TEXT UNIQUE,
    min_score   INTEGER NOT NULL DEFAULT 75,
    watchlist   BOOLEAN NOT NULL DEFAULT true,
    stops       BOOLEAN NOT NULL DEFAULT true,
    last_check  TIMESTAMPTZ NOT NULL DEFAULT now(),
    linked_at   TIMESTAMPTZ
);
