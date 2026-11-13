-- Greffe de la Chambre : file des déclarations PTR et lien vers le document source

CREATE TABLE IF NOT EXISTS house_filings (
    doc_id      TEXT PRIMARY KEY,
    year        INTEGER NOT NULL,
    name        TEXT,
    state_dst   TEXT,
    filing_date DATE,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'error', 'skipped')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    trades      INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS house_filings_pending ON house_filings (status, filing_date DESC) WHERE status = 'pending';

ALTER TABLE congress_trades ADD COLUMN IF NOT EXISTS url TEXT;
