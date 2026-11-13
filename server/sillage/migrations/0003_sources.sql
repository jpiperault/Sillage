-- Form 144, AMF, 13F, FINRA

-- Form 144 : ventes annoncées par les initiés (avant la vente)
CREATE TABLE IF NOT EXISTS sale_notices (
    accession          TEXT PRIMARY KEY,
    issuer_cik         BIGINT,
    issuer_name        TEXT,
    ticker             TEXT,
    person             TEXT,
    relationship       TEXT,
    security_class     TEXT,
    shares             NUMERIC,
    market_value       NUMERIC,
    shares_outstanding NUMERIC,
    approx_sale_date   DATE,
    filed_date         DATE,
    plan_10b5_1        BOOLEAN NOT NULL DEFAULT false,
    url                TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sale_notices_filed ON sale_notices (filed_date DESC);
CREATE INDEX IF NOT EXISTS sale_notices_ticker ON sale_notices (ticker, filed_date DESC);

-- AMF : déclarations des dirigeants français
CREATE TABLE IF NOT EXISTS amf_filings (
    numero     TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    company    TEXT,
    published  DATE,
    status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'error')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    trades     INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS amf_filings_pending ON amf_filings (status, published DESC) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS amf_trades (
    id         BIGSERIAL PRIMARY KEY,
    numero     TEXT NOT NULL,
    line_no    INTEGER NOT NULL,
    company    TEXT,
    person     TEXT NOT NULL,
    role       TEXT,
    side       TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    isin       TEXT,
    price      NUMERIC,
    currency   TEXT,
    volume     NUMERIC,
    value      NUMERIC,
    trade_date DATE,
    published  DATE,
    venue      TEXT,
    nature     TEXT,
    url        TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (numero, line_no)
);
CREATE INDEX IF NOT EXISTS amf_trades_published ON amf_trades (published DESC);
CREATE INDEX IF NOT EXISTS amf_trades_isin ON amf_trades (isin, trade_date DESC);

-- 13F : portefeuilles trimestriels des grands fonds suivis
CREATE TABLE IF NOT EXISTS fund_filings (
    accession   TEXT PRIMARY KEY,
    cik         BIGINT NOT NULL,
    fund_name   TEXT,
    period      DATE NOT NULL,
    filed_date  DATE,
    total_value NUMERIC,
    positions   INTEGER,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fund_filings_cik ON fund_filings (cik, period DESC);

CREATE TABLE IF NOT EXISTS fund_holdings (
    accession TEXT NOT NULL REFERENCES fund_filings(accession) ON DELETE CASCADE,
    cusip     TEXT NOT NULL,
    put_call  TEXT NOT NULL DEFAULT '',
    issuer    TEXT,
    title     TEXT,
    shares    NUMERIC,
    value     NUMERIC,
    PRIMARY KEY (accession, cusip, put_call)
);

CREATE TABLE IF NOT EXISTS cusip_tickers (
    cusip      TEXT PRIMARY KEY,
    ticker     TEXT,
    name       TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- FINRA : volume vendu à découvert, quotidien
CREATE TABLE IF NOT EXISTS short_volume (
    day          DATE NOT NULL,
    ticker       TEXT NOT NULL,
    short_volume NUMERIC NOT NULL,
    total_volume NUMERIC NOT NULL,
    PRIMARY KEY (ticker, day)
);
CREATE INDEX IF NOT EXISTS short_volume_day ON short_volume (day);
