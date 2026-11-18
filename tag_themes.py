#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Marvel OS - theme-tagging LLM pour les fiches issues de HubSpot.

HubSpot ne pose pas de themes sur ses resumes de calls. Ce script range chaque fiche
`source='hubspot_call'` SANS theme dans le MEME vocabulaire canonique anglais que les
fiches Chorus (THEME_CANON), via une courte passe Claude contrainte par enum.

Proprietes :
  - N'agit QUE sur les fiches dont `themes` est vide -> idempotent, reexecutable, et
    les nouveaux calls (ingeres chaque jour) se font tagger tout seuls a la passe suivante.
  - Ecrit les libelles canoniques dans documents.themes ET reconstruit document_themes
    pour ces documents (le trigger Postgres rafraichit tsv automatiquement a l'UPDATE).

Prerequis : ANTHROPIC_API_KEY (+ ANTHROPIC_WORKSPACE_ID si cle user), DATABASE_URL.
DRY_RUN=1 pour simuler sans ecrire. Ne log JAMAIS de secret.
"""
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_env_local():
    f = HERE / ".env.local"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if m:
            os.environ.setdefault(m.group(1), m.group(2).strip().strip('"').strip("'"))


_load_env_local()
if os.environ.get("DATABASE_URL_UNPOOLED"):
    os.environ["DATABASE_URL"] = os.environ["DATABASE_URL_UNPOOLED"]

import anthropic                        # noqa: E402
import db                               # noqa: E402
from ingest_chorus import THEME_CANON   # noqa: E402  (source unique du vocabulaire)

if not db.IS_POSTGRES:
    raise SystemExit("[!] DATABASE_URL (Neon/Postgres) requis pour ce script")
if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
    raise SystemExit("[!] ANTHROPIC_API_KEY manquant (.env.local / secret)")

CANON = sorted(set(THEME_CANON.values()))
MODEL = os.environ.get("MARVEL_TAG_MODEL", "claude-sonnet-4-6")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

TOOL = {
    "name": "assign_themes",
    "description": "Return the 1-5 canonical themes that best describe this sales-call summary.",
    "input_schema": {
        "type": "object",
        "properties": {
            "themes": {
                "type": "array",
                "items": {"type": "string", "enum": CANON},
                "description": "Only labels from the fixed vocabulary, most relevant first, no new themes.",
            }
        },
        "required": ["themes"],
    },
}

SYS = (
    "You classify a B2B sales/technical call summary (Marvel Buildings: modular permanent "
    "kitchens & mobile rental kitchen trailers) into a FIXED English theme vocabulary. "
    "Pick the 1-5 themes that are ACTUALLY discussed in the summary. Use ONLY the provided "
    "labels; never invent a theme. If unsure, pick fewer rather than guessing."
)


def make_client():
    """Client Anthropic (+ header workspace si cle user non scoped)."""
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    if ws:
        return anthropic.Anthropic(default_headers={"anthropic-workspace-id": ws})
    return anthropic.Anthropic()


def tag_one(client, title, body):
    text = f"TITLE: {title}\n\nSUMMARY:\n{(body or '')[:6000]}"
    resp = client.messages.create(
        model=MODEL, max_tokens=300,
        system=SYS, tools=[TOOL],
        tool_choice={"type": "tool", "name": "assign_themes"},
        messages=[{"role": "user", "content": text}],
    )
    for b in resp.content:
        if b.type == "tool_use" and b.name == "assign_themes":
            raw = b.input.get("themes", []) or []
            out, seen = [], set()
            for t in raw:
                if t in CANON and t not in seen:
                    seen.add(t)
                    out.append(t)
            return out[:5]
    return []


def main():
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, title, body FROM documents "
                "WHERE source='hubspot_call' AND (themes IS NULL OR themes='') "
                "ORDER BY date")
    todo = cur.fetchall()
    print(f"[INFO] {len(todo)} fiche(s) hubspot_call sans theme a tagger (modele {MODEL}).")
    if not todo:
        conn.close()
        return

    client = make_client()
    tagged = 0
    for doc_id, title, body in todo:
        themes = tag_one(client, title or "", body or "")
        if not themes:
            print(f"  [SKIP] #{doc_id} {str(title)[:50]} -> aucun theme retourne")
            continue
        joined = ";".join(themes)
        if DRY_RUN:
            print(f"  #{doc_id} {str(title)[:46]:46} -> {joined}")
            continue
        cur.execute("UPDATE documents SET themes=%s WHERE id=%s", (joined, doc_id))
        cur.execute("DELETE FROM document_themes WHERE document_id=%s", (doc_id,))
        for t in themes:
            cur.execute("INSERT INTO document_themes (document_id, theme) VALUES (%s, %s)",
                        (doc_id, t))
        tagged += 1

    if not DRY_RUN:
        conn.commit()
    conn.close()
    verb = "simule" if DRY_RUN else "tagge"
    print(f"[FAIT] {tagged} fiche(s) {verb} (themes canoniques -> documents.themes + document_themes).")


if __name__ == "__main__":
    main()
