-- Sillage : schéma initial

CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin      BOOLEAN NOT NULL DEFAULT false,
    token_version INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS cik_tickers (
    cik        BIGINT PRIMARY KEY,
    ticker     TEXT NOT NULL,
    title      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cik_tickers_ticker ON cik_tickers (ticker);

-- File de traitement des dépôts Form 4
CREATE TABLE IF NOT EXISTS sec_filings (
    accession  TEXT PRIMARY KEY,
    form_type  TEXT NOT NULL,
    url        TEXT NOT NULL,
    filed_date DATE,
    status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'error')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sec_filings_pending ON sec_filings (status, filed_date DESC) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS insider_trades (
    id               BIGSERIAL PRIMARY KEY,
    accession        TEXT NOT NULL,
    line_no          INTEGER NOT NULL,
    issuer_cik       BIGINT,
    issuer_name      TEXT,
    ticker           TEXT,
    owner_name       TEXT NOT NULL,
    owner_cik        BIGINT,
    is_director      BOOLEAN NOT NULL DEFAULT false,
    is_officer       BOOLEAN NOT NULL DEFAULT false,
    is_ten_percent   BOOLEAN NOT NULL DEFAULT false,
    officer_title    TEXT,
    code             TEXT NOT NULL,
    acquired         BOOLEAN,
    shares           NUMERIC,
    price            NUMERIC,
    value            NUMERIC,
    shares_after     NUMERIC,
    trade_date       DATE,
    filed_date       DATE,
    plan_10b5_1      BOOLEAN NOT NULL DEFAULT false,
    url              TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (accession, line_no)
);
CREATE INDEX IF NOT EXISTS insider_trades_filed ON insider_trades (filed_date DESC);
CREATE INDEX IF NOT EXISTS insider_trades_ticker ON insider_trades (ticker, trade_date DESC);

-- 13D / 13G (sans lecture du document)
CREATE TABLE IF NOT EXISTS ownership_filings (
    accession     TEXT PRIMARY KEY,
    form_type     TEXT NOT NULL,
    subject_cik   BIGINT,
    subject_name  TEXT,
    ticker        TEXT,
    filer_cik     BIGINT,
    filer_name    TEXT,
    filed_date    DATE,
    url           TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ownership_filed ON ownership_filings (filed_date DESC);

CREATE TABLE IF NOT EXISTS congress_trades (
    id                BIGSERIAL PRIMARY KEY,
    source            TEXT NOT NULL,
    source_key        TEXT NOT NULL,
    politician        TEXT NOT NULL,
    party             TEXT,
    chamber           TEXT,
    ticker            TEXT,
    side              TEXT NOT NULL,
    amount_range      TEXT,
    amount_min        NUMERIC,
    amount_max        NUMERIC,
    trade_date        DATE,
    report_date       DATE,
    raw               JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, source_key)
);
CREATE INDEX IF NOT EXISTS congress_trades_report ON congress_trades (report_date DESC);

CREATE TABLE IF NOT EXISTS prices (
    ticker     TEXT PRIMARY KEY,
    price      NUMERIC NOT NULL,
    currency   TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS paper_accounts (
    user_id    BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    cash       NUMERIC NOT NULL,
    start_cash NUMERIC NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reset_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS paper_positions (
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ticker     TEXT NOT NULL,
    quantity   NUMERIC NOT NULL,
    avg_price  NUMERIC NOT NULL,
    stop_price NUMERIC,
    signal_ref TEXT,
    opened_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, ticker)
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ticker     TEXT NOT NULL,
    side       TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity   NUMERIC NOT NULL,
    price      NUMERIC NOT NULL,
    pnl        NUMERIC,
    reason     TEXT NOT NULL DEFAULT 'manual' CHECK (reason IN ('manual', 'stop')),
    signal_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS paper_orders_user ON paper_orders (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS watchlist (
    user_id  BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ticker   TEXT NOT NULL,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, ticker)
);

CREATE TABLE IF NOT EXISTS job_state (
    name          TEXT PRIMARY KEY,
    last_run_at   TIMESTAMPTZ,
    last_ok_at    TIMESTAMPTZ,
    last_error_at TIMESTAMPTZ,
    last_error    TEXT,
    info          JSONB NOT NULL DEFAULT '{}'::jsonb
);
