#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Marvel OS - connecteur source #4 : calls HubSpot (AI summary) -> Neon.

Regle (validee avec John, 2026-11-18) :
  - On NE touche PAS aux fiches Chorus existantes.
  - On ingere uniquement les calls HubSpot qui ont un AI summary dans hs_call_body
    ET dont la date est POSTERIEURE a la derniere fiche Chorus (forward-only).
  - SUMMARY uniquement (on jette le TRANSCRIPT verbatim).

Idempotent (upsert sur (source, source_id) ; source_id = id du call HubSpot).
Reexecutable a volonte : chaque nouveau call summarise rentre tout seul.

Marque : via le pipeline du deal associe (comme ingest_hubspot.py).
Lit HUBSPOT_ACCESS_TOKEN + DATABASE_URL depuis l'environnement / .env.local.
Ne log JAMAIS de secret.
"""
import os
import re
import csv  # noqa: F401  (reserve)
import json
import html
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
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

TOKEN = os.environ.get("HUBSPOT_ACCESS_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("[!] HUBSPOT_ACCESS_TOKEN manquant (.env.local)")

import db  # noqa: E402

if not db.IS_POSTGRES:
    raise SystemExit("[!] DATABASE_URL (Neon/Postgres) requis pour ce connecteur")

HDR = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
PIPELINE_BRAND = {"default": "modular", "71756716": "mobile"}
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")


# ----------------------------------------------------------------- HubSpot API
def _get(url):
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=HDR, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _assoc_first(call_id, target):
    try:
        res = _get(f"https://api.hubapi.com/crm/v3/objects/calls/{call_id}/associations/{target}")
        r = res.get("results", [])
        return r[0]["id"] if r else None
    except urllib.error.HTTPError:
        return None


def _owners_map():
    out = {}
    for o in _get("https://api.hubapi.com/crm/v3/owners?limit=100").get("results", []):
        out[str(o.get("id"))] = f"{o.get('firstName', '')} {o.get('lastName', '')}".strip()
    return out


# ----------------------------------------------------------------- parsing
def extract_summary(raw):
    """Garde la partie SUMMARY, jette le TRANSCRIPT. None si pas de vrai summary."""
    if not raw:
        return None
    t = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    t = re.sub(r"(?i)</(p|div|li|ul|ol|h[1-6])>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    # couper avant le transcript verbatim
    t = re.split(r"(?i)\bTRANSCRIPT\s*:", t, maxsplit=1)[0]
    if "SUMMARY" not in t.upper():
        return None
    t = re.sub(r"(?i)^\s*SUMMARY\s*:\s*", "", t)          # vire le label
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if not t:
        return None
    head = t[:300].lower()
    # Ecarte les messages d'echec de l'IA (recap non genere faute de transcript fourni).
    if any(x in head for x in ("i don't see", "i do not see", "i don’t see",
                               "i appreciate you", "no transcript", "there's no transcript",
                               "didn't include", "did not include")):
        return None
    # Garde-fou positif : un vrai recap contient au moins une section structuree.
    if not re.search(r"(?i)(topics discussed|pain points|next steps|objections|outcome)", t):
        return None
    return t


# ----------------------------------------------------------------- DB upsert
COLS = ["source", "source_id", "doc_type", "title", "date", "entity",
        "brand", "status", "themes", "body", "raw_meta", "updated_at"]


def upsert(conn, vals):
    ph = ", ".join(["%s"] * len(COLS))
    # On NE touche PAS a `themes` sur conflit : il est pose par le tagger LLM (tag_themes.py)
    # apres l'ingestion. Sinon chaque run quotidien l'ecraserait a '' et re-taggerait pour rien.
    keep = ("source", "source_id", "themes")
    sql = (f"INSERT INTO documents ({', '.join(COLS)}) VALUES ({ph}) "
           f"ON CONFLICT (source, source_id) DO UPDATE SET "
           + ", ".join(f"{c}=EXCLUDED.{c}" for c in COLS if c not in keep))
    cur = conn.cursor()
    cur.execute(sql, vals)


def main():
    # 1) cutoff = derniere fiche Chorus (forward-only, pas de doublon)
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT MAX(date) FROM documents WHERE source='chorus'")
    cutoff = cur.fetchone()[0] or "2026-09-18"
    cutoff_ms = int(datetime.strptime(str(cutoff)[:10], "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc).timestamp() * 1000)
    print(f"[INFO] cutoff (derniere fiche Chorus) = {cutoff} ; on ingere les calls summaries APRES.")

    owners = _owners_map()
    # deals deja en base -> brand/dealname (reutilise le connecteur deals)
    cur.execute("SELECT hs_id, brand, dealname FROM deals")
    deals = {r[0]: {"brand": r[1], "dealname": r[2]} for r in cur.fetchall()}

    # 2) pull des calls summaries posterieurs au cutoff
    props = ["hs_timestamp", "hs_call_title", "hs_call_body", "hubspot_owner_id", "hs_call_direction"]
    body = {
        "limit": 100,
        "sorts": [{"propertyName": "hs_timestamp", "direction": "ASCENDING"}],
        "properties": props,
        "filterGroups": [{"filters": [
            {"propertyName": "hs_call_body", "operator": "CONTAINS_TOKEN", "value": "SUMMARY"},
            {"propertyName": "hs_timestamp", "operator": "GT", "value": str(cutoff_ms)},
        ]}],
    }
    results, after = [], None
    while True:
        if after:
            body["after"] = after
        data = _post("https://api.hubapi.com/crm/v3/objects/calls/search", body)
        results.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.12)

    print(f"[INFO] {len(results)} call(s) candidat(s) (summary + apres cutoff).")
    now = datetime.now(timezone.utc).isoformat()
    kept = 0
    for c in results:
        p = c.get("properties", {})
        summ = extract_summary(p.get("hs_call_body"))
        if not summ:
            continue
        cid = c["id"]
        ts = p.get("hs_timestamp", "")
        date = ts[:10] if ts else ""
        deal_id = _assoc_first(cid, "deals")
        dmeta = deals.get(deal_id, {})
        # entite : societe associee > nom du deal > titre du call
        entity = ""
        comp_id = _assoc_first(cid, "companies")
        if comp_id:
            try:
                entity = _get(f"https://api.hubapi.com/crm/v3/objects/companies/{comp_id}"
                              f"?properties=name")["properties"].get("name") or ""
            except urllib.error.HTTPError:
                entity = ""
        if not entity:
            entity = dmeta.get("dealname") or ""
        title = (p.get("hs_call_title") or "").strip() \
            or next((ln.lstrip("# ").strip() for ln in summ.splitlines() if ln.strip()), cid)
        # Marque : le titre/recap nomme explicitement la marque. Plus fiable que
        # le pipeline du deal (tous les deals sont dans le pipeline "Sales" = modular).
        blob = f"{title} {summ}".lower()
        mob, mod = "mobile culinaire" in blob, "modular culinaire" in blob
        brand = "mobile" if mob and not mod else "modular" if mod and not mob \
            else ("multi" if mob and mod else "")
        raw_meta = json.dumps({
            "hs_call_id": cid, "owner": owners.get(str(p.get("hubspot_owner_id")), ""),
            "deal_id": deal_id, "direction": p.get("hs_call_direction"),
            "source": "hubspot_call_ai_summary",
        }, ensure_ascii=False)
        vals = ["hubspot_call", cid, "call_sheet", title, date, entity,
                brand, "fait", "", summ, raw_meta, now]
        kept += 1
        if DRY_RUN:
            print(f"  [{date}] brand={brand or '?':7} | {entity[:28]:28} | {title[:40]}")
            print(f"           {summ[:160].replace(chr(10), ' ')}...")
        else:
            upsert(conn, vals)

    if not DRY_RUN:
        conn.commit()
    conn.close()
    verb = "simule" if DRY_RUN else "ingere"
    print(f"\n[FAIT] {kept} call(s) HubSpot {verb} (source=hubspot_call, summary only).")


if __name__ == "__main__":
    main()
