#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Marvel OS — couche base de données (source-agnostique).

Moteur par défaut : SQLite + FTS5 (zéro install, le POC tourne immédiatement).
Cible prod : PostgreSQL — définis DATABASE_URL=postgresql+psycopg2://user:pwd@host/marvel_os
Le reste du code (ingestion, agent) ne change pas : seule la connexion + la requête FTS
branchent sur le dialecte.

Table canonique `documents` : conçue pour accueillir N sources (Chorus aujourd'hui, HubSpot /
Gmail / Ads demain). C'est ÇA le "own our data" : un seul store relationnel, exportable.
"""
import os
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SQLITE = HERE / "marvel_os.db"

# postgresql+psycopg2://... => Postgres ; sinon SQLite local
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_POSTGRES = DATABASE_URL.startswith("postgres")


# ---------------------------------------------------------------------------
# Connexion
# ---------------------------------------------------------------------------
def get_conn(readonly: bool = False):
    """
    Retourne une connexion DB-API.
    readonly=True : sous Postgres, utilise le rôle marvel_ro si MARVEL_RO_URL est défini ;
    sous SQLite, ouvre en mode lecture seule (file: ... ?mode=ro).
    """
    if IS_POSTGRES:
        import psycopg2
        url = os.environ.get("MARVEL_RO_URL", DATABASE_URL) if readonly else DATABASE_URL
        # SQLAlchemy-style URL -> psycopg2 accepte l'URL libpq directement si on retire le driver
        libpq = url.replace("postgresql+psycopg2://", "postgresql://")
        return psycopg2.connect(libpq)
    # SQLite
    if readonly and DEFAULT_SQLITE.exists():
        uri = f"file:{DEFAULT_SQLITE}?mode=ro"
        return sqlite3.connect(uri, uri=True)
    conn = sqlite3.connect(DEFAULT_SQLITE)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def dialect() -> str:
    return "postgres" if IS_POSTGRES else "sqlite"


# ---------------------------------------------------------------------------
# Schéma
# ---------------------------------------------------------------------------
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,              -- 'chorus', 'hubspot', ...
    source_id     TEXT NOT NULL,              -- nom_fichier / id natif de la source
    doc_type      TEXT,                       -- 'call_sheet', 'deal', 'email', ...
    title         TEXT,
    date          TEXT,
    entity        TEXT,                       -- entreprise / compte
    brand         TEXT,                       -- mobile / modular / multi / marvel
    status        TEXT,                       -- fait / vide / ...
    themes        TEXT,                       -- tags séparés par ';'
    body          TEXT,                       -- contenu intégral de la fiche
    raw_meta      TEXT,                       -- JSON brut (métadonnées source)
    updated_at    TEXT,
    UNIQUE(source, source_id)
);
-- Index full-text FTS5 (externe, synchronisé par triggers)
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title, body, entity, themes,
    content='documents', content_rowid='id',
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, title, body, entity, themes)
    VALUES (new.id, new.title, new.body, new.entity, new.themes);
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, body, entity, themes)
    VALUES ('delete', old.id, old.title, old.body, old.entity, old.themes);
END;
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, body, entity, themes)
    VALUES ('delete', old.id, old.title, old.body, old.entity, old.themes);
    INSERT INTO documents_fts(rowid, title, body, entity, themes)
    VALUES (new.id, new.title, new.body, new.entity, new.themes);
END;

-- Thème = dimension de première classe (un thème = une ligne), reconstruite à l'ingestion.
CREATE TABLE IF NOT EXISTS document_themes (
    document_id INTEGER NOT NULL,
    theme       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS document_themes_theme_idx ON document_themes(theme);
CREATE INDEX IF NOT EXISTS document_themes_doc_idx   ON document_themes(document_id);

-- Conversations du front (historique durable, suit la base -> jamais perdu au redeploy).
CREATE TABLE IF NOT EXISTS chats (
    id         TEXT PRIMARY KEY,
    title      TEXT,
    scope      TEXT,
    messages   TEXT,
    updated_at TEXT
);
"""


def init_schema():
    """Crée le schéma. SQLite seulement (Postgres : voir schema.sql)."""
    if IS_POSTGRES:
        raise SystemExit(
            "Sous Postgres, applique schema.sql à la main (psql -f schema.sql).\n"
            "Le POC SQLite s'initialise automatiquement."
        )
    conn = get_conn()
    try:
        conn.executescript(SQLITE_SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Recherche full-text (branche sur le dialecte)
# ---------------------------------------------------------------------------
def full_text_search(query, brand=None, date_from=None, date_to=None, entity=None, limit=10):
    """
    Recherche lexicale sur title+body(+entity+themes). Filtres optionnels par colonne.
    Retourne une liste de dicts : source_id, title, date, brand, entity, snippet.
    """
    conn = get_conn(readonly=True)
    try:
        cur = conn.cursor()
        filters, params = [], []
        if brand:
            filters.append("d.brand = ?")
            params.append(brand)
        if entity:
            filters.append("d.entity LIKE ?")
            params.append(f"%{entity}%")
        if date_from:
            filters.append("d.date >= ?")
            params.append(date_from)
        if date_to:
            filters.append("d.date <= ?")
            params.append(date_to)

        if IS_POSTGRES:
            where = " AND ".join(["d.tsv @@ plainto_tsquery('english', %s)"] + [f.replace("?", "%s") for f in filters])
            sql = f"""
                SELECT d.source_id, d.title, d.date, d.brand, d.entity,
                       ts_headline('english', d.body, plainto_tsquery('english', %s),
                                   'MaxWords=30, MinWords=10') AS snippet
                FROM documents d
                WHERE {where}
                ORDER BY ts_rank(d.tsv, plainto_tsquery('english', %s)) DESC
                LIMIT %s
            """
            # Ordre des %s dans le SQL : ts_headline(query), WHERE match(query),
            # filtres(params), ts_rank(query), LIMIT. D'où cet ordre exact :
            cur.execute(sql, [query, query] + params + [query, limit])
        else:
            where = " AND ".join(["documents_fts MATCH ?"] + filters)
            sql = f"""
                SELECT d.source_id, d.title, d.date, d.brand, d.entity,
                       snippet(documents_fts, 1, '[', ']', ' … ', 12) AS snippet
                FROM documents_fts
                JOIN documents d ON d.id = documents_fts.rowid
                WHERE {where}
                ORDER BY rank
                LIMIT ?
            """
            cur.execute(sql, [query] + params + [limit])

        cols = ["source_id", "title", "date", "brand", "entity", "snippet"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def get_document(source_id, source="chorus"):
    """Corps intégral d'une fiche (context stuffing ciblé)."""
    conn = get_conn(readonly=True)
    try:
        cur = conn.cursor()
        ph = "%s" if IS_POSTGRES else "?"
        cur.execute(
            f"SELECT source_id, title, date, brand, entity, themes, body "
            f"FROM documents WHERE source_id = {ph} AND source = {ph}",
            (source_id, source),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = ["source_id", "title", "date", "brand", "entity", "themes", "body"]
        return dict(zip(cols, row))
    finally:
        conn.close()


def themes_breakdown(brand=None, limit=50):
    """Thèmes canoniques × nombre de documents (option : filtré par marque).
    S'appuie sur la table normalisée document_themes (vocabulaire canonique EN)."""
    conn = get_conn(readonly=True)
    try:
        cur = conn.cursor()
        ph = "%s" if IS_POSTGRES else "?"
        if brand:
            cur.execute(
                f"SELECT dt.theme, COUNT(*) AS n "
                f"FROM document_themes dt JOIN documents d ON d.id = dt.document_id "
                f"WHERE d.brand = {ph} GROUP BY dt.theme ORDER BY n DESC LIMIT {ph}",
                (brand, limit),
            )
        else:
            cur.execute(
                f"SELECT dt.theme, COUNT(*) AS n FROM document_themes dt "
                f"GROUP BY dt.theme ORDER BY n DESC LIMIT {ph}",
                (limit,),
            )
        return [{"theme": r[0], "n": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()


def run_select(sql, limit=200):
    """
    Exécute une requête SELECT en lecture seule (garde : rejette tout non-SELECT).
    Utilisé par l'outil text-to-SQL de l'agent.
    """
    stripped = sql.strip().rstrip(";").strip()
    low = stripped.lower()
    if not low.startswith("select") and not low.startswith("with"):
        raise ValueError("Seules les requêtes SELECT/WITH sont autorisées.")
    forbidden = (" insert ", " update ", " delete ", " drop ", " alter ", " create ",
                 " truncate ", " grant ", " attach ", " pragma ")
    padded = f" {low} "
    if any(tok in padded for tok in forbidden):
        raise ValueError("Mot-clé de modification détecté — requête refusée.")

    conn = get_conn(readonly=True)
    try:
        cur = conn.cursor()
        cur.execute(stripped)
        cols = [c[0] for c in cur.description] if cur.description else []
        rows = cur.fetchmany(limit)
        return {"columns": cols, "rows": [list(r) for r in rows], "truncated": len(rows) == limit}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Conversations du front (historique durable : Neon en prod, SQLite en local).
# On utilise une connexion READ-WRITE (DATABASE_URL) : ce sont les données propres de
# l'app, pas la source de vérité exposée à l'agent (lui reste sandboxé via run_select).
# ---------------------------------------------------------------------------
def ensure_chats_table():
    """Crée la table chats si absente (idempotent, les deux dialectes)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        if IS_POSTGRES:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS chats ("
                " id TEXT PRIMARY KEY, title TEXT, scope TEXT,"
                " messages JSONB, updated_at TIMESTAMPTZ DEFAULT now())")
        else:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS chats ("
                " id TEXT PRIMARY KEY, title TEXT, scope TEXT,"
                " messages TEXT, updated_at TEXT)")
        conn.commit()
    finally:
        conn.close()


def save_chat(chat_id, title, scope, messages):
    """Upsert d'une conversation (messages = liste de dicts role/content/tools)."""
    payload = json.dumps(messages, ensure_ascii=False)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = get_conn()
    try:
        cur = conn.cursor()
        if IS_POSTGRES:
            cur.execute(
                "INSERT INTO chats (id, title, scope, messages, updated_at) "
                "VALUES (%s, %s, %s, %s, now()) "
                "ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, scope=EXCLUDED.scope, "
                "messages=EXCLUDED.messages, updated_at=now()",
                (chat_id, title, scope, payload))
        else:
            cur.execute(
                "INSERT INTO chats (id, title, scope, messages, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, scope=excluded.scope, "
                "messages=excluded.messages, updated_at=excluded.updated_at",
                (chat_id, title, scope, payload, now))
        conn.commit()
    finally:
        conn.close()


def list_chats(limit=200):
    """Conversations (récentes d'abord) : id, title, scope, updated."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if IS_POSTGRES else "?"
        cur.execute(
            f"SELECT id, title, scope, updated_at FROM chats "
            f"ORDER BY updated_at DESC LIMIT {ph}", (limit,))
        return [{"id": r[0], "title": r[1], "scope": r[2], "updated": str(r[3])[:19]}
                for r in cur.fetchall()]
    finally:
        conn.close()


def load_chat(chat_id):
    """Messages d'une conversation (liste), [] si absente."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if IS_POSTGRES else "?"
        cur.execute(f"SELECT messages FROM chats WHERE id = {ph}", (chat_id,))
        row = cur.fetchone()
        if not row or row[0] is None:
            return []
        data = row[0]
        return data if isinstance(data, list) else json.loads(data)
    finally:
        conn.close()


def delete_chat(chat_id):
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if IS_POSTGRES else "?"
        cur.execute(f"DELETE FROM chats WHERE id = {ph}", (chat_id,))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_schema()
    print(f"[OK] schéma initialisé ({dialect()}) -> {DEFAULT_SQLITE if not IS_POSTGRES else DATABASE_URL}")
