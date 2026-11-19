#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Marvel OS — connecteur source #1 : fiches de synthèse des calls Chorus.

Lit yassine-kb/index.csv (métadonnées) + yassine-kb/fiches/<nom>.md (corps),
upsert idempotent dans la table `documents`. Ne charge que statut == 'fait'.
Réexécutable à volonté (ex. après la réécriture EN des fiches).
"""
import sys, io, csv, json, os, collections
from datetime import datetime, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", write_through=True)

import db  # couche base locale

KB = Path(r"C:\Users\johni\yassine-kb")
INDEX = KB / "index.csv"
FICHES = KB / "fiches"

# ---------------------------------------------------------------------------
# Vocabulaire canonique des thèmes (anglais, aligné sur AGENT_INSTRUCTIONS.md).
# Chaque clé est la forme FR *normalisée* (sans accent, séparateurs ramenés à '/',
# minuscule). Fusionne les variantes (électricité/electricite, normes/permis vs
# normes-permis, stratégie/strategie…) -> UN libellé propre, comptable, filtrable.
# C'est ce qui règle les "doublons de vocabulaire" évoqués.
# ---------------------------------------------------------------------------
THEME_CANON = {
    "relation client": "client relations",
    "conception": "design",
    "normes/permis": "codes/permits",
    "installation": "installation",
    "pricing": "pricing",
    "transport/livraison": "transportation/delivery",
    "ventilation/hottes": "ventilation/hoods",
    "plomberie": "plumbing",
    "marketing/prospection": "marketing/prospecting",
    "electricite": "electrical",
    "production": "production",
    "strategie": "strategy",
    "refrigeration": "refrigeration",
    "materiaux": "materials",
    "documentation/production": "documentation/production",
    "documentation/plans": "documentation/production",
    "gaz": "gas",
    "fournisseurs": "suppliers",
    "maintenance": "maintenance",
    "recrutement/equipe": "recruitment/team",
    "juridique/contrats": "legal/contracts",
    "sav": "after-sales service",
    "sanitaire/wc": "sanitary/restrooms",
    "sanitaire/toilettes": "sanitary/restrooms",
    # Alias : variantes anglaises / ad-hoc qui ont dérivé du vocabulaire contrôlé.
    "permitting": "codes/permits",
    "plumbing": "plumbing",
    "electrical": "electrical",
    "sanitaires": "sanitary/restrooms",
    "sanitaire": "sanitary/restrooms",
    "ventilation": "ventilation/hoods",
    "materiau": "materials",
    "delivery": "transportation/delivery",
    "equipement cuisine": "kitchen equipment",
    "equipement": "kitchen equipment",
    "strategie commerciale": "strategy",
    "strategic projects": "strategy",
    "planification": "timeline/planning",
    "timeline/planning": "timeline/planning",
    "timeline": "timeline/planning",
}

_ACCENTS = str.maketrans("àâäéèêëîïôöûüùçñ", "aaaeeeeiioouuucn")
CANON_VALUES = set(THEME_CANON.values())   # libellés EN déjà canoniques


def canon_theme(raw):
    """Étiquette FR brute (accents / séparateurs /- / casse) -> libellé canonique EN,
    ou None si le thème n'est pas au vocabulaire (loggé, jamais inséré en silence).
    Idempotent : un libellé DÉJÀ canonique (ex. 'client relations' posé par le tagger
    LLM sur les fiches HubSpot) est renvoyé tel quel -> préservé à chaque rebuild."""
    s = raw.strip()
    if s in CANON_VALUES:
        return s
    k = s.lower().translate(_ACCENTS).replace("-", "/")
    k = " ".join(k.split())            # espaces multiples
    k = k.replace(" / ", "/").replace("/ ", "/").replace(" /", "/")
    return THEME_CANON.get(k)


def canon_join(raw):
    """Tags bruts (FR, encodage douteux, separateurs ';'/',' mixtes) -> libelles canoniques
    EN, dedoublonnes, joints par ';'. Les tags hors vocabulaire sont ecartes (comme pour
    document_themes). But : que documents.themes soit en anglais propre, comme la table
    normalisee, et le reste a chaque re-ingestion (plus de francais/mojibake residuel)."""
    seen, out = set(), []
    for t in (raw or "").replace(",", ";").split(";"):
        c = canon_theme(t.strip())
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return ";".join(out)


def ascii_name(s):
    repl = {"ç": "c", "é": "e", "è": "e", "ê": "e", "ë": "e", "à": "a", "â": "a", "ä": "a",
            "î": "i", "ï": "i", "ô": "o", "ö": "o", "û": "u", "ü": "u", "ù": "u", "ñ": "n",
            "İ": "I", "ı": "i", "ş": "s", "ğ": "g", "ʻ": "", "’": "", chr(0x2018): ""}
    for k, v in repl.items():
        s = s.replace(k, v)
    return s


def find_fiche(nom):
    allmd = {f.name for f in FICHES.glob("*.md")}
    for cand in (f"{nom}.md", f"{ascii_name(nom)}.md"):
        if cand in allmd:
            return FICHES / cand
    return None


def upsert(conn, row, body):
    ph = "%s" if db.IS_POSTGRES else "?"
    cols = ["source", "source_id", "doc_type", "title", "date", "entity",
            "brand", "status", "themes", "body", "raw_meta", "updated_at"]
    now = datetime.now(timezone.utc).isoformat()
    title = (body.splitlines()[0].lstrip("# ").strip() if body else row["nom_fichier"])
    vals = [
        "chorus", row["nom_fichier"], "call_sheet", title, row.get("date", ""),
        row.get("entreprise", ""), (row.get("marque", "") or "").strip(),
        row.get("statut", "").strip(), canon_join(row.get("themes", "")), body,
        json.dumps({k: row.get(k, "") for k in
                    ("owner", "type_call", "duree_min_est", "folder_id", "transcript_id", "notes")},
                   ensure_ascii=False),
        now,
    ]
    placeholders = ", ".join([ph] * len(cols))
    if db.IS_POSTGRES:
        sql = (f"INSERT INTO documents ({', '.join(cols)}) VALUES ({placeholders}) "
               f"ON CONFLICT (source, source_id) DO UPDATE SET "
               + ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in ("source", "source_id")))
    else:
        sql = (f"INSERT INTO documents ({', '.join(cols)}) VALUES ({placeholders}) "
               f"ON CONFLICT(source, source_id) DO UPDATE SET "
               + ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("source", "source_id")))
    cur = conn.cursor()            # curseur : marche sur SQLite ET psycopg2
    cur.execute(sql, vals)


def main():
    if not db.IS_POSTGRES:
        db.init_schema()

    rows = list(csv.DictReader(open(INDEX, encoding="utf-8-sig")))
    fait = [r for r in rows if r.get("statut", "").strip() == "fait"]
    print(f"[INFO] index: {len(rows)} entrées | statut=fait: {len(fait)}")

    conn = db.get_conn()
    ok = skip = 0
    try:
        for r in fait:
            fp = find_fiche(r["nom_fichier"])
            if not fp:
                skip += 1
                print(f"  [SKIP] fiche introuvable: {r['nom_fichier']}")
                continue
            body = fp.read_text(encoding="utf-8", errors="replace")
            upsert(conn, r, body)
            ok += 1
        conn.commit()
    finally:
        conn.close()

    # Reconstruit la table normalisée des thèmes : split sur ';' (pas '/'), mapping
    # vers le vocabulaire canonique EN, dédoublonnage par document (deux variantes qui
    # fusionnent ne créent qu'une ligne).
    ins = ("INSERT INTO document_themes (document_id, theme) VALUES (%s, %s)"
           if db.IS_POSTGRES else
           "INSERT INTO document_themes (document_id, theme) VALUES (?, ?)")
    conn = db.get_conn()
    unknown = collections.Counter()
    try:
        cur = conn.cursor()        # curseur : marche sur SQLite ET psycopg2
        cur.execute("DELETE FROM document_themes")
        cur.execute("SELECT id, themes FROM documents")
        n_links = 0
        for doc_id, themes in cur.fetchall():
            seen = set()
            # Le CSV mélange les séparateurs ';' et ',' -> on normalise avant split.
            for t in (themes or "").replace(",", ";").split(";"):
                t = t.strip()
                if not t or t == "-" or t.lower() == "[à extraire]":
                    continue  # placeholder "thème non encore extrait", pas un vrai tag
                canon = canon_theme(t)
                if canon is None:
                    unknown[t] += 1
                    continue
                if canon in seen:          # variantes qui fusionnent -> une seule ligne
                    continue
                seen.add(canon)
                cur.execute(ins, (doc_id, canon))
                n_links += 1
        conn.commit()
    finally:
        conn.close()

    print(f"[FAIT] {ok} fiches ingérées, {skip} ignorées, {n_links} liens thèmes ({db.dialect()}).")
    if unknown:
        print(f"[WARN] {sum(unknown.values())} tags thèmes hors vocabulaire (non insérés) : "
              f"{dict(unknown.most_common())}")

    # Vérif rapide
    res = db.run_select("SELECT brand, COUNT(*) AS n FROM documents GROUP BY brand ORDER BY n DESC")
    print("\nRépartition par marque dans le store :")
    for brand, n in res["rows"]:
        print(f"  {brand or '(vide)':10} {n}")


if __name__ == "__main__":
    main()
