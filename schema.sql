-- Marvel OS — schéma PostgreSQL (cible production)
-- Le POC tourne par défaut sur SQLite (auto-créé par db.py).
-- Pour passer en Postgres : créer la base, appliquer ce fichier, exporter DATABASE_URL.
--
--   createdb marvel_os
--   psql -d marvel_os -f schema.sql
--   set DATABASE_URL=postgresql+psycopg2://marvel:motdepasse@localhost/marvel_os   (Windows)
--
-- pgvector reste en réserve : `CREATE EXTENSION vector;` + une colonne embedding le jour
-- où des requêtes réelles prouvent que le lexical ne suffit plus (recherche hybride).

CREATE TABLE IF NOT EXISTS documents (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source      TEXT NOT NULL,              -- 'chorus', 'hubspot', 'gmail', 'ads', ...
    source_id   TEXT NOT NULL,              -- id natif de la source
    doc_type    TEXT,                       -- 'call_sheet', 'deal', 'email', ...
    title       TEXT,
    date        DATE,
    entity      TEXT,                       -- entreprise / compte
    brand       TEXT,                       -- mobile / modular / multi / marvel
    status      TEXT,
    themes      TEXT,
    body        TEXT,
    raw_meta    JSONB,
    updated_at  TIMESTAMPTZ DEFAULT now(),
    tsv         TSVECTOR,
    UNIQUE (source, source_id)
);

-- Index full-text (generated tsvector + GIN)
CREATE OR REPLACE FUNCTION documents_tsv_update() RETURNS trigger AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', coalesce(NEW.title,  '')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.entity, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(NEW.themes, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(NEW.body,   '')), 'C');
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS documents_tsv_trg ON documents;
CREATE TRIGGER documents_tsv_trg BEFORE INSERT OR UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION documents_tsv_update();

CREATE INDEX IF NOT EXISTS documents_tsv_gin ON documents USING GIN (tsv);
CREATE INDEX IF NOT EXISTS documents_brand_idx ON documents (brand);
CREATE INDEX IF NOT EXISTS documents_date_idx  ON documents (date);
CREATE INDEX IF NOT EXISTS documents_entity_idx ON documents (entity);

-- Thème = dimension de première classe (un thème = une ligne), reconstruite à l'ingestion
-- à partir d'un vocabulaire canonique anglais (cf. THEME_CANON dans ingest_chorus.py).
-- C'est ce qui élimine les "doublons de vocabulaire" (électricité vs electricite) et rend
-- le thème filtrable/comptable à égalité avec la marque.
CREATE TABLE IF NOT EXISTS document_themes (
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    theme       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS document_themes_theme_idx ON document_themes (theme);
CREATE INDEX IF NOT EXISTS document_themes_doc_idx   ON document_themes (document_id);

-- Conversations du front : historique DURABLE (suit la base, jamais perdu au redeploy
-- du conteneur cloud, contrairement au stockage disque ephemere). Donnees propres de
-- l'app (pas exposees a l'agent en lecture seule). Cree aussi a chaud par db.ensure_chats_table().
CREATE TABLE IF NOT EXISTS chats (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    scope       TEXT,
    messages    JSONB,
    updated_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chats_updated_idx ON chats (updated_at DESC);

-- Gouvernance : rôle LECTURE SEULE pour l'agent (argument sécurité pour la réunion).
-- L'agent text-to-SQL se connecte via ce rôle -> impossible de muter la source de vérité.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marvel_ro') THEN
        CREATE ROLE marvel_ro LOGIN PASSWORD 'readonly';
    END IF;
END$$;
GRANT CONNECT ON DATABASE marvel_os TO marvel_ro;
GRANT USAGE ON SCHEMA public TO marvel_ro;
GRANT SELECT ON documents TO marvel_ro;
GRANT SELECT ON document_themes TO marvel_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO marvel_ro;

-- ---------------------------------------------------------------------------
-- ITÉRATION 2 (non créées ici) : tables normalisées par source, reliées à documents.
--   deals(id, source_id, stage, amount, close_date, owner, entity, ...)
--   contacts(id, source_id, email, role, entity, ...)
--   calls(id, document_id FK, duration_min, participants, ...)
-- L'entity resolution (même client across Chorus/HubSpot/Gmail) se fait ici.
-- ---------------------------------------------------------------------------
