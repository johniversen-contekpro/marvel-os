#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration one-shot : SQLite local -> Neon Postgres.

Lit DATABASE_URL_UNPOOLED depuis .env.local (connexion directe, recommandée pour le DDL
et l'ingestion), applique le schéma Postgres, puis relance l'ingestion des 327 fiches.
N'imprime JAMAIS la connection string.
"""
import os
import re
from pathlib import Path

ENV = Path(__file__).resolve().parent / ".env.local"


def load_env_local():
    vals = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if m:
            vals[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return vals


def scrub(msg):
    """Supprime toute URL postgres d'un message d'erreur avant affichage."""
    return re.sub(r"postgres(?:ql)?://[^\s'\"]+", "postgresql://<redacted>", str(msg))


# DDL Postgres : miroir du schéma SQLite (colonnes TEXT, robustes aux données réelles)
# + full-text natif (tsvector maintenu par trigger + index GIN). Pas de rôle/grant ici
# (gouvernance = étape suivante), pas de nom de base en dur (Neon = 'neondb').
POSTGRES_DDL = """
CREATE TABLE IF NOT EXISTS documents (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    doc_type    TEXT,
    title       TEXT,
    date        TEXT,
    entity      TEXT,
    brand       TEXT,
    status      TEXT,
    themes      TEXT,
    body        TEXT,
    raw_meta    TEXT,
    updated_at  TEXT,
    tsv         TSVECTOR,
    UNIQUE (source, source_id)
);
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
CREATE INDEX IF NOT EXISTS documents_brand_idx  ON documents (brand);
CREATE INDEX IF NOT EXISTS documents_date_idx   ON documents (date);
CREATE INDEX IF NOT EXISTS documents_entity_idx ON documents (entity);
CREATE TABLE IF NOT EXISTS document_themes (
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    theme       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS document_themes_theme_idx ON document_themes (theme);
CREATE INDEX IF NOT EXISTS document_themes_doc_idx   ON document_themes (document_id);
"""


def main():
    env = load_env_local()
    url = env.get("DATABASE_URL_UNPOOLED") or env.get("DATABASE_URL")
    if not url or not url.startswith("postgres"):
        raise SystemExit("[!] Pas d'URL Postgres dans .env.local")
    os.environ["DATABASE_URL"] = url  # db.py lira ceci et basculera en mode Postgres

    import db
    assert db.IS_POSTGRES, "db n'est pas en mode Postgres"

    # 1) schéma
    conn = db.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(POSTGRES_DDL)
        conn.commit()
    finally:
        conn.close()
    print("[OK] schéma Postgres appliqué sur Neon")

    # 2) ingestion (réutilise exactement le connecteur existant)
    import ingest_chorus
    ingest_chorus.main()

    # 3) vérifications
    docs = db.run_select("SELECT COUNT(*) FROM documents")["rows"][0][0]
    links = db.run_select("SELECT COUNT(*) FROM document_themes")["rows"][0][0]
    fts = [r["source_id"] for r in db.full_text_search("permitting", brand="modular", limit=2)]
    print(f"[VERIF] documents={docs} | liens thèmes={links} | FTS test={fts}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        raise SystemExit("[ERREUR] " + scrub(e))
