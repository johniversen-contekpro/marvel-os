#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Marvel OS - migration one-shot : conversations disque (chats/*.json) -> table chats (Neon).

Cree la table chats si absente, puis importe l'historique local existant pour qu'aucune
conversation ne soit perdue au passage disque -> base. Idempotent (upsert sur id).
"""
import os
import re
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
for line in (HERE / ".env.local").read_text(encoding="utf-8").splitlines():
    m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
    if m:
        os.environ.setdefault(m.group(1), m.group(2).strip().strip('"').strip("'"))
if os.environ.get("DATABASE_URL_UNPOOLED"):
    os.environ["DATABASE_URL"] = os.environ["DATABASE_URL_UNPOOLED"]

import db  # noqa: E402

db.ensure_chats_table()

cdir = HERE / "chats"
migrated = 0
if cdir.exists():
    for f in sorted(cdir.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        msgs = d.get("messages", [])
        if not msgs:
            continue
        db.save_chat(d.get("id") or f.stem, (d.get("title") or "")[:60],
                     d.get("scope") or "all", msgs)
        migrated += 1

total = db.run_select("SELECT COUNT(*) FROM chats")["rows"][0][0]
print(f"[OK] table chats prete (Neon) ; {migrated} conversation(s) migree(s) depuis le disque.")
print(f"[INFO] {total} conversation(s) en base.")
