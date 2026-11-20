#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Marvel OS - eval leger (regression harness).

But : apres chaque changement (prompt, modele, schema, connecteur), verifier en une
commande que l'agent ne regresse pas sur nos vrais risques :
  - text-to-SQL faux (chiffres) -> checks DETERMINISTES : on compare la reponse de
    l'agent a une verite terrain calculee en SQL (recalculee a chaque run, donc valable
    meme quand la donnee evolue).
  - sources hallucinees -> on verifie que les source_id cites EXISTENT vraiment.
  - fuite de marque (brand bleed) -> sous un scope, aucune source citee ne doit appartenir
    a l'autre marque.

Ce n'est PAS une plateforme d'eval : ~15 questions golden, lancables en local / CI.
Prerequis : ANTHROPIC_API_KEY (+ WORKSPACE_ID si cle user), DATABASE_URL.
Usage : python eval.py            (tout)
        python eval.py -v         (montre le SQL genere par l'agent sur les echecs)
"""
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for line in (HERE / ".env.local").read_text(encoding="utf-8").splitlines() if (HERE / ".env.local").exists() else []:
    m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
    if m:
        os.environ.setdefault(m.group(1), m.group(2).strip().strip('"').strip("'"))
if os.environ.get("DATABASE_URL_UNPOOLED"):
    os.environ["DATABASE_URL"] = os.environ["DATABASE_URL_UNPOOLED"]

import db                     # noqa: E402
import marvel_agent as agent  # noqa: E402

VERBOSE = "-v" in sys.argv


def gt(sql):
    """Verite terrain : scalaire d'un SELECT (recalcule a chaque run)."""
    return db.run_select(sql)["rows"][0][0]


# source_id -> brand (pour les checks sources / etancheite).
# limit haut : run_select plafonne a 200 par defaut -> il FAUT tout charger (sinon des
# fiches reelles citees par l'agent manquent et on crie au loup a tort).
SRC_BRAND = {str(sid): (b or "") for sid, b in
             db.run_select("SELECT source_id, brand FROM documents", limit=100000)["rows"] if sid}


def extract_numbers(text):
    """Nombres mentionnes, suffixes k/m/b/million... resolus, separateurs de milliers retires."""
    t = re.sub(r"(?<=\d),(?=\d)", "", text)
    out = []
    for m in re.finditer(r"\$?\s*([0-9]+(?:\.[0-9]+)?)\s*(billion|million|thousand|k|m|b)?", t, re.I):
        mult = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6,
                "b": 1e9, "billion": 1e9}.get((m.group(2) or "").lower(), 1)
        out.append(float(m.group(1)) * mult)
    return out


def cited_source_ids(answer):
    """source_id reels cites, en gerant les citations TRONQUEES : l'agent cite une forme
    courte (date + entite, ex. `2026-07-14_PGAL`) alors que le source_id complet contient
    le proprietaire + un hash. On matche donc par PREFIXE."""
    toks = set(re.findall(r"\d{4}-\d{2}-\d{2}_[A-Za-z0-9_]+", answer))   # chorus : YYYY-MM-DD_Entity...
    toks |= set(re.findall(r"(?<!\d)\d{8,}(?!\d)", answer))             # hubspot_call : id numerique
    hits = set()
    for t in toks:
        for sid in SRC_BRAND:
            if sid == t or sid.startswith(t) or t.startswith(sid):
                hits.add(sid)
                break
    return list(hits)


# --------------------------------------------------------------- cas numeriques
# (question, scope, sql_verite, exact?) ; exact=True -> comptage, match strict.
NUM_CASES = [
    ("How many deals are in the pipeline in total?", None,
     "SELECT COUNT(*) FROM deals", True),
    ("What is the total pipeline value across all deals, in dollars?", None,
     "SELECT SUM(amount) FROM deals", False),
    ("How many deals are tagged with the Mobile brand?", None,
     "SELECT COUNT(*) FROM deals WHERE brand='mobile'", True),
    ("What is the total pipeline value of Mobile-brand deals, in dollars?", None,
     "SELECT SUM(amount) FROM deals WHERE brand='mobile'", False),
    ("How many deals are tagged with the Modular brand?", None,
     "SELECT COUNT(*) FROM deals WHERE brand='modular'", True),
    ("How many contacts are in the CRM in total?", None,
     "SELECT COUNT(*) FROM contacts", True),
    ("How many knowledge call sheets do we have in total, across all sources?", None,
     "SELECT COUNT(*) FROM documents", True),
    ("How many call sheets are tagged mobile?", None,
     "SELECT COUNT(*) FROM documents WHERE brand='mobile'", True),
    ("How many call sheets are tagged modular?", None,
     "SELECT COUNT(*) FROM documents WHERE brand='modular'", True),
    # Scope applique aux DEALS : en scope mobile, 'how many deals' doit se restreindre a mobile.
    ("How many deals do we have?", "mobile",
     "SELECT COUNT(*) FROM deals WHERE brand='mobile'", True),
]

# ------------------------------------------------------------- cas qualitatifs
# type 'sources' : doit citer au moins une source reelle.
# type 'purity'  : sous scope, aucune source citee ne doit etre de l'autre marque.
# type 'refusal' : la donnee demandee N'EXISTE PAS -> l'agent doit l'admettre, pas inventer.
QUAL_CASES = [
    ("sources", "Which objections recur on permitting? Cite the specific calls.", None),
    ("sources", "Summarize what customers ask for in recent Mobile Culinaire discovery calls. Cite the calls.", None),
    ("purity", "What topics come up most across our calls? Cite a few calls.", "mobile"),
    ("purity", "What are the recurring themes in our calls? Cite a few calls.", "modular"),
    # Pas de champ CSAT ni d'age client en base -> doit refuser proprement.
    ("refusal", "What is our average customer satisfaction (CSAT) score across all deals?", None),
    ("refusal", "What is the average age of our contacts, in years?", None),
]

# Signaux d'un refus honnete (la donnee n'est pas dans le store).
_REFUSAL_RE = re.compile(
    r"(?i)(don'?t have|do not have|not (?:available|captured|tracked|stored|present|in the data|in our data)|"
    r"no (?:data|information|field|column|record|csat|age)|"
    r"isn'?t (?:captured|tracked|available|stored)|doesn'?t (?:have|contain|include|track|capture)|"
    r"not (?:something|a field|recorded)|cannot (?:be )?(?:computed|calculated|determined|found)|"
    r"unable to|no way to (?:compute|determine)|there is no)")

OTHER = {"mobile": {"modular", "marvel"}, "modular": {"mobile", "marvel"}}


def run_num(q, scope, sql, exact):
    expected = gt(sql)
    if expected is None:
        return None, "verite terrain NULL", ""
    expected = float(expected)
    tools = []
    ans = agent.ask(q, verbose=False, scope_brand=scope,
                    on_tool=lambda n, i: tools.append(i.get("sql", "")))
    vals = extract_numbers(ans)
    if exact:
        ok = any(abs(v - expected) < 0.5 for v in vals)
    else:
        ok = any(abs(v - expected) <= max(0.02 * abs(expected), 1) for v in vals)
    detail = f"attendu~{expected:,.0f} ; trouve {[f'{v:,.0f}' for v in vals][:6]}"
    return ok, detail, "\n".join(t for t in tools if t)


def run_qual(kind, q, scope):
    calls = []
    ans = agent.ask(q, verbose=False, scope_brand=scope,
                    on_tool=lambda n, i: calls.append((n, i)))
    sqls = "\n".join(i.get("sql", "") for n, i in calls if n == "sql_query" and i.get("sql"))
    # Ancrage = sources CITEES (prose, fragile) UNION sources OUVERTES via get_document
    # (deterministe, insensible au format de citation). On teste que la reponse est ancree
    # dans de vraies fiches, pas la forme exacte de la citation.
    retrieved = {str(i.get("source_id")) for n, i in calls
                 if n == "get_document" and str(i.get("source_id")) in SRC_BRAND}
    grounded = set(cited_source_ids(ans)) | retrieved
    if kind == "refusal":
        ok = bool(_REFUSAL_RE.search(ans))
        return ok, f"refus honnete={ok} ; '{' '.join(ans.split())[:90]}...'", sqls
    if kind == "sources":
        ok = len(grounded) >= 1
        return ok, f"{len(grounded)} source(s) reelle(s) (ouvertes {len(retrieved)})", sqls
    # purity : aucune source ancree (citee OU ouverte) ne doit etre de l'autre marque
    bad = [c for c in grounded if SRC_BRAND.get(c) in OTHER.get(scope, set())]
    ok = len(bad) == 0
    return ok, f"{len(grounded)} source(s), scope={scope}, hors-marque={bad[:5]}", sqls


def main():
    print("=== Marvel OS eval ===\n")
    passed = total = 0
    fails = []

    print("-- Numeriques (verite terrain SQL) --")
    for q, scope, sql, exact in NUM_CASES:
        total += 1
        ok, detail, sqls = run_num(q, scope, sql, exact)
        tag = "PASS" if ok else "FAIL"
        passed += 1 if ok else 0
        sc = f" [scope={scope}]" if scope else ""
        print(f"  [{tag}]{sc} {q}\n         {detail}")
        if not ok:
            fails.append((q, detail, sqls))

    print("\n-- Qualitatifs (sources reelles / etancheite marque) --")
    for kind, q, scope in QUAL_CASES:
        total += 1
        ok, detail, sqls = run_qual(kind, q, scope)
        tag = "PASS" if ok else "FAIL"
        passed += 1 if ok else 0
        sc = f" [scope={scope}]" if scope else ""
        print(f"  [{tag}][{kind}]{sc} {q}\n         {detail}")
        if not ok:
            fails.append((q, detail, sqls))

    print(f"\n=== {passed}/{total} PASS ===")
    if VERBOSE and fails:
        print("\n--- SQL genere sur les echecs ---")
        for q, detail, sqls in fails:
            print(f"\nQ: {q}\n   {detail}\n   SQL:\n{sqls or '   (aucun)'}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
