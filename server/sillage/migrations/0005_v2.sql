-- v2 : capitalisation, rachats, résultats, cours européens, BaFin, Sénat, contrats publics,
--      track record, modèle de score, portefeuilles fictifs multiples (manuel / règle), courbe de valeur

-- Actions en circulation (SEC XBRL frames) → capitalisation = actions × cours
CREATE TABLE IF NOT EXISTS company_shares (
    cik        BIGINT PRIMARY KEY,
    shares     NUMERIC NOT NULL,
    as_of      DATE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Annonces de rachat d'actions (8-K, recherche plein texte SEC)
CREATE TABLE IF NOT EXISTS buybacks (
    accession TEXT PRIMARY KEY,
    cik       BIGINT NOT NULL,
    company   TEXT,
    filed     DATE NOT NULL
);
CREATE INDEX IF NOT EXISTS buybacks_cik ON buybacks (cik, filed DESC);

-- Dates de publication des résultats (calendrier Nasdaq)
CREATE TABLE IF NOT EXISTS earnings (
    ticker TEXT NOT NULL,
    day    DATE NOT NULL,
    timing TEXT,
    PRIMARY KEY (ticker, day)
);
CREATE INDEX IF NOT EXISTS earnings_day ON earnings (day);
CREATE TABLE IF NOT EXISTS earnings_days_done (day DATE PRIMARY KEY, fetched_at TIMESTAMPTZ NOT NULL DEFAULT now());

-- Taux de change de la BCE (EUR → USD) pour le fictif en actions européennes
CREATE TABLE IF NOT EXISTS fx_rates (
    currency TEXT PRIMARY KEY,
    per_eur  NUMERIC NOT NULL,
    day      DATE NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Allemagne : transactions des dirigeants (BaFin)
CREATE TABLE IF NOT EXISTS bafin_trades (
    id         BIGSERIAL PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    company    TEXT,
    isin       TEXT,
    person     TEXT NOT NULL,
    role       TEXT,
    side       TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    price      NUMERIC,
    currency   TEXT,
    value      NUMERIC,
    volume     NUMERIC,
    trade_date DATE,
    published  DATE,
    venue      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bafin_trades_published ON bafin_trades (published DESC);

-- Contrats publics fédéraux par société (USAspending), en cache
CREATE TABLE IF NOT EXISTS gov_contracts (
    ticker      TEXT PRIMARY KEY,
    search_name TEXT,
    amount_12m  NUMERIC,
    recipients  JSONB,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Sénat : file des déclarations
CREATE TABLE IF NOT EXISTS senate_filings (
    report_id  TEXT PRIMARY KEY,
    senator    TEXT,
    filed      DATE,
    url        TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'error', 'skipped')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Déclarations corrigées (4/A) : trace des remplacements
ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS amended_by TEXT;
ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS is_amendment BOOLEAN NOT NULL DEFAULT false;

-- Track record par personne (calculé depuis le backtest et les élus)
CREATE TABLE IF NOT EXISTS track_records (
    kind       TEXT NOT NULL,             -- insider | congress
    person_key TEXT NOT NULL,             -- CIK de l'initié ou nom de l'élu
    name       TEXT,
    n          INTEGER NOT NULL,
    wins       INTEGER NOT NULL,
    mean_63    NUMERIC,
    median_63  NUMERIC,
    last_trade DATE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, person_key)
);

-- Modèle de score v2 (régression logistique entraînée sur le backtest)
CREATE TABLE IF NOT EXISTS score_models (
    id         BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    side       TEXT NOT NULL,
    model      JSONB NOT NULL,
    metrics    JSONB NOT NULL,
    active     BOOLEAN NOT NULL DEFAULT false
);

-- Portefeuilles fictifs multiples : « manual » (ordres à la main) et « rule » (règle automatique)
ALTER TABLE paper_accounts ADD COLUMN IF NOT EXISTS book TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS book TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE paper_orders ADD COLUMN IF NOT EXISTS book TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'USD';
ALTER TABLE paper_orders DROP CONSTRAINT IF EXISTS paper_orders_reason_check;
ALTER TABLE paper_orders ADD CONSTRAINT paper_orders_reason_check CHECK (reason IN ('manual', 'stop', 'rule', 'horizon'));
ALTER TABLE paper_orders ADD COLUMN IF NOT EXISTS note TEXT;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'paper_accounts_pkey'
               AND pg_get_constraintdef(oid) NOT LIKE '%book%') THEN
        ALTER TABLE paper_accounts DROP CONSTRAINT paper_accounts_pkey;
        ALTER TABLE paper_accounts ADD PRIMARY KEY (user_id, book);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'paper_positions_pkey'
               AND pg_get_constraintdef(oid) NOT LIKE '%book%') THEN
        ALTER TABLE paper_positions DROP CONSTRAINT paper_positions_pkey;
        ALTER TABLE paper_positions ADD PRIMARY KEY (user_id, book, ticker);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS paper_rules (
    user_id       BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    enabled       BOOLEAN NOT NULL DEFAULT false,
    min_score     INTEGER NOT NULL DEFAULT 80,
    alloc_pct     NUMERIC NOT NULL DEFAULT 2,
    hold_days     INTEGER NOT NULL DEFAULT 90,          -- jours calendaires (~3 mois)
    max_positions INTEGER NOT NULL DEFAULT 30,
    sources       TEXT NOT NULL DEFAULT 'insider',
    last_check    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Valeur quotidienne des portefeuilles fictifs (courbe)
CREATE TABLE IF NOT EXISTS paper_equity (
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book    TEXT NOT NULL,
    day     DATE NOT NULL,
    equity  NUMERIC NOT NULL,
    cash    NUMERIC NOT NULL,
    PRIMARY KEY (user_id, book, day)
);

-- Résumé quotidien par Telegram (optionnel)
ALTER TABLE user_alerts ADD COLUMN IF NOT EXISTS digest BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE user_alerts ADD COLUMN IF NOT EXISTS digest_sent DATE;
