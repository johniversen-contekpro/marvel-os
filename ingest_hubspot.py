#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Marvel OS — connecteur source #2 : HubSpot (deals + contacts) -> Neon.

Idempotent (upsert sur l'id HubSpot). Réexécutable à volonté.
Lit HUBSPOT_ACCESS_TOKEN + DATABASE_URL depuis l'environnement / .env.local.
Ne log JAMAIS de secret.

Signal de marque (règle métier, John 2026-11-18) : dans HubSpot tout est sous une seule
marque/pipeline, la distinction se lit sur le PRODUIT via le champ deal `type_of_product` :
  - contient "Trailer" (Mobile Kitchen Rental) -> brand 'mobile'
  - tout le reste (Kitchen/Restroom/Other, ou vide) -> brand 'modular'
Le pipeline, lui, ne distingue pas les marques (tous les deals sont dans "Sales").
"""
import os
import re
import json
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
# Connexion directe (non poolée) préférée pour l'ingestion en masse.
if os.environ.get("DATABASE_URL_UNPOOLED"):
    os.environ["DATABASE_URL"] = os.environ["DATABASE_URL_UNPOOLED"]

TOKEN = os.environ.get("HUBSPOT_ACCESS_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("[!] HUBSPOT_ACCESS_TOKEN manquant (ajoute-le dans .env.local)")

import db  # noqa: E402

if not db.IS_POSTGRES:
    raise SystemExit("[!] DATABASE_URL (Neon/Postgres) requis pour ce connecteur")

HDR = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def brand_from_product(type_of_product):
    """Marque réelle d'un deal depuis `type_of_product` (multi-select ';').
    Règle : un produit Trailer = Mobile Kitchen Rental -> 'mobile' ; sinon 'modular'
    (Kitchen/Restroom/Other, ou champ vide = 'le reste')."""
    toks = {t.strip().lower() for t in (type_of_product or "").split(";") if t.strip()}
    return "mobile" if "trailer" in toks else "modular"

DDL = """
CREATE TABLE IF NOT EXISTS deals (
    hs_id       TEXT PRIMARY KEY,
    dealname    TEXT,
    amount      NUMERIC,
    brand       TEXT,              -- mobile (type_of_product ~ Trailer) / modular (rest)
    pipeline    TEXT,              -- libellé de pipeline
    stage       TEXT,              -- libellé d'étape résolu
    stage_id    TEXT,
    closedate   TEXT,
    createdate  TEXT,
    owner_id    TEXT,
    raw         TEXT,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS deals_brand_idx ON deals (brand);
CREATE INDEX IF NOT EXISTS deals_stage_idx ON deals (stage);

CREATE TABLE IF NOT EXISTS contacts (
    hs_id          TEXT PRIMARY KEY,
    email          TEXT,
    firstname      TEXT,
    lastname       TEXT,
    company        TEXT,
    jobtitle       TEXT,
    lifecyclestage TEXT,
    createdate     TEXT,
    raw            TEXT,
    updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS contacts_email_idx ON contacts (email);
"""


def _api_get(url):
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _paginate(obj, props):
    after, out = None, []
    while True:
        url = (f"https://api.hubapi.com/crm/v3/objects/{obj}"
               f"?limit=100&properties={','.join(props)}")
        if after:
            url += f"&after={after}"
        data = _api_get(url)
        out.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            return out
        time.sleep(0.12)  # respect des rate limits HubSpot


def _pipelines_map():
    data = _api_get("https://api.hubapi.com/crm/v3/pipelines/deals")
    stage_lbl, pipe_lbl = {}, {}
    for p in data.get("results", []):
        pipe_lbl[p["id"]] = p.get("label")
        for s in p.get("stages", []):
            stage_lbl[s["id"]] = s.get("label")
    return stage_lbl, pipe_lbl


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def main():
    now = datetime.now(timezone.utc).isoformat()
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(DDL)
    conn.commit()

    stage_lbl, pipe_lbl = _pipelines_map()

    # ---- deals ----
    dprops = ["dealname", "amount", "dealstage", "pipeline", "type_of_product",
              "closedate", "createdate", "hubspot_owner_id"]
    deals = _paginate("deals", dprops)
    for x in deals:
        p = x.get("properties", {})
        pid = p.get("pipeline") or ""
        cur.execute(
            "INSERT INTO deals (hs_id,dealname,amount,brand,pipeline,stage,stage_id,"
            "closedate,createdate,owner_id,raw,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (hs_id) DO UPDATE SET dealname=EXCLUDED.dealname, amount=EXCLUDED.amount, "
            "brand=EXCLUDED.brand, pipeline=EXCLUDED.pipeline, stage=EXCLUDED.stage, "
            "stage_id=EXCLUDED.stage_id, closedate=EXCLUDED.closedate, createdate=EXCLUDED.createdate, "
            "owner_id=EXCLUDED.owner_id, raw=EXCLUDED.raw, updated_at=EXCLUDED.updated_at",
            (x["id"], p.get("dealname"), _f(p.get("amount")), brand_from_product(p.get("type_of_product")),
             pipe_lbl.get(pid, pid), stage_lbl.get(p.get("dealstage"), p.get("dealstage")),
             p.get("dealstage"), p.get("closedate"), p.get("createdate"),
             p.get("hubspot_owner_id"), json.dumps(p, ensure_ascii=False), now),
        )
    conn.commit()

    # ---- contacts ----
    cprops = ["email", "firstname", "lastname", "company",
              "jobtitle", "lifecyclestage", "createdate"]
    contacts = _paginate("contacts", cprops)
    for x in contacts:
        p = x.get("properties", {})
        cur.execute(
            "INSERT INTO contacts (hs_id,email,firstname,lastname,company,jobtitle,"
            "lifecyclestage,createdate,raw,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (hs_id) DO UPDATE SET email=EXCLUDED.email, firstname=EXCLUDED.firstname, "
            "lastname=EXCLUDED.lastname, company=EXCLUDED.company, jobtitle=EXCLUDED.jobtitle, "
            "lifecyclestage=EXCLUDED.lifecyclestage, createdate=EXCLUDED.createdate, "
            "raw=EXCLUDED.raw, updated_at=EXCLUDED.updated_at",
            (x["id"], p.get("email"), p.get("firstname"), p.get("lastname"), p.get("company"),
             p.get("jobtitle"), p.get("lifecyclestage"), p.get("createdate"),
             json.dumps(p, ensure_ascii=False), now),
        )
    conn.commit()
    conn.close()

    print(f"[FAIT] deals={len(deals)} | contacts={len(contacts)} -> Neon ({db.dialect()})")
    print("\nDeals par marque (nb, pipeline value $) :")
    for row in db.run_select(
        "SELECT COALESCE(brand,'(?)') AS brand, COUNT(*) AS n, "
        "COALESCE(SUM(amount),0) AS total FROM deals GROUP BY brand ORDER BY n DESC"
    )["rows"]:
        print(f"  {row[0]:10} n={row[1]:4}  pipeline=${row[2]:,.0f}")


if __name__ == "__main__":
    main()
