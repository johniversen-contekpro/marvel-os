#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Marvel OS - backfill "B" : calls HubSpot (AI summary) ANTERIEURS au cutoff Chorus,
pour lesquels il n'existe PAS deja une fiche Chorus (dedup entreprise + jour).

Le connecteur forward (ingest_hubspot_calls.py) ne prend que les calls APRES la derniere
fiche Chorus. Ce script comble l'historique : les summaries d'avant le cutoff qui n'ont
jamais ete transformes en fiche a la main.

Dedup : on saute un call si une fiche Chorus existe le MEME jour pour la MEME entreprise
(entites normalisees, l'une contient l'autre). Idempotent (upsert sur source_id = id du call,
source='hubspot_call' : meme espace que le forward -> aucun double-compte, reexecutable).
SUMMARY uniquement (on jette le TRANSCRIPT), memes regles de parsing que le forward.

One-shot historique : ce script N'est PAS dans le workflow quotidien (lui ne fait que le forward).
DRY_RUN=1 pour simuler. Lit HUBSPOT_ACCESS_TOKEN + DATABASE_URL depuis .env.local.
"""
import os
import re
import json
import time
from collections import defaultdict
from datetime import datetime, timezone

import ingest_hubspot_calls as fwd   # reutilise API HubSpot + parsing + upsert + COLS
import db

DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"inc", "llc", "ltd", "co", "corp", "company", "group", "the", "usa", "and", "of"}


def norm_entity(s):
    """Nom d'entreprise -> forme comparable (minuscule, sans suffixes juridiques)."""
    toks = [w for w in _WORD.findall((s or "").lower()) if w not in _STOP and len(w) > 1]
    return " ".join(toks)


def main():
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT MAX(date) FROM documents WHERE source='chorus'")
    cutoff = cur.fetchone()[0] or "2026-09-18"
    cutoff_ms = int(datetime.strptime(str(cutoff)[:10], "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc).timestamp() * 1000)
    print(f"[INFO] cutoff (derniere fiche Chorus) = {cutoff} ; backfill des summaries AVANT/LE jour-la.")

    # Index des fiches Chorus existantes : date -> set d'entites normalisees (dedup).
    cur.execute("SELECT entity, date FROM documents WHERE source='chorus'")
    chorus_by_date = defaultdict(set)
    for ent, d in cur.fetchall():
        ne = norm_entity(ent)
        if ne:
            chorus_by_date[str(d)[:10]].add(ne)

    owners = fwd._owners_map()
    cur.execute("SELECT hs_id, brand, dealname FROM deals")
    deals = {r[0]: {"brand": r[1], "dealname": r[2]} for r in cur.fetchall()}

    # Pull des calls summaries <= cutoff.
    props = ["hs_timestamp", "hs_call_title", "hs_call_body", "hubspot_owner_id", "hs_call_direction"]
    body = {
        "limit": 100,
        "sorts": [{"propertyName": "hs_timestamp", "direction": "ASCENDING"}],
        "properties": props,
        "filterGroups": [{"filters": [
            {"propertyName": "hs_call_body", "operator": "CONTAINS_TOKEN", "value": "SUMMARY"},
            {"propertyName": "hs_timestamp", "operator": "LTE", "value": str(cutoff_ms)},
        ]}],
    }
    results, after = [], None
    while True:
        if after:
            body["after"] = after
        data = fwd._post("https://api.hubapi.com/crm/v3/objects/calls/search", body)
        results.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.12)
    print(f"[INFO] {len(results)} call(s) candidat(s) (summary + <= cutoff).")

    now = datetime.now(timezone.utc).isoformat()
    kept = dup = nosum = unlinked = 0
    for c in results:
        p = c.get("properties", {})
        summ = fwd.extract_summary(p.get("hs_call_body"))
        if not summ:
            nosum += 1
            continue
        cid = c["id"]
        ts = p.get("hs_timestamp", "")
        date = ts[:10] if ts else ""
        deal_id = fwd._assoc_first(cid, "deals")
        comp_id = fwd._assoc_first(cid, "companies")
        # Garde-fou qualite : un vrai call CLIENT est rattache a un deal ou une entreprise
        # dans HubSpot. Les entretiens de recrutement / admin interne / coordination ne le
        # sont pas -> on les ecarte (on ne pollue pas la source de verite comme les fiches
        # Chorus triees a la main).
        if not deal_id and not comp_id:
            unlinked += 1
            time.sleep(0.05)
            continue
        dmeta = deals.get(deal_id, {})
        # entite : societe associee > nom du deal > titre du call
        entity = ""
        if comp_id:
            try:
                entity = fwd._get(f"https://api.hubapi.com/crm/v3/objects/companies/{comp_id}"
                                  f"?properties=name")["properties"].get("name") or ""
            except Exception:
                entity = ""
        if not entity:
            entity = dmeta.get("dealname") or ""
        title = (p.get("hs_call_title") or "").strip() \
            or next((ln.lstrip("# ").strip() for ln in summ.splitlines() if ln.strip()), cid)

        # Dedup vs fiches Chorus : meme jour + entite qui recoupe une fiche existante.
        ne = norm_entity(entity)
        existing = chorus_by_date.get(date, set())
        if ne and any(ne in ce or ce in ne for ce in existing):
            dup += 1
            time.sleep(0.07)
            continue

        # Marque depuis le texte. Pre-rebrand : "ContekPro" = la marque modular.
        blob = f"{title} {summ}".lower()
        mob = "mobile culinaire" in blob
        mod = "modular culinaire" in blob or "contekpro" in blob or "contek pro" in blob
        brand = "mobile" if mob and not mod else "modular" if mod and not mob \
            else ("multi" if mob and mod else "")
        raw_meta = json.dumps({
            "hs_call_id": cid, "owner": owners.get(str(p.get("hubspot_owner_id")), ""),
            "deal_id": deal_id, "direction": p.get("hs_call_direction"),
            "source": "hubspot_call_ai_summary", "backfill": True,
        }, ensure_ascii=False)
        vals = ["hubspot_call", cid, "call_sheet", title, date, entity,
                brand, "fait", "", summ, raw_meta, now]
        kept += 1
        if DRY_RUN:
            print(f"  [{date}] {brand or '?':7} | {entity[:26]:26} | {title[:40]}")
        else:
            fwd.upsert(conn, vals)
        time.sleep(0.07)

    if not DRY_RUN:
        conn.commit()
    conn.close()
    verb = "simule" if DRY_RUN else "ingere"
    print(f"\n[FAIT] {kept} call(s) backfill {verb} | {dup} doublon(s) Chorus ignore(s) | "
          f"{unlinked} non rattache(s) (interne/recrutement) | {nosum} sans vrai summary.")


if __name__ == "__main__":
    main()
