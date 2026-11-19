#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Marvel OS — agent outillé (pas de RAG).

Un agent Claude qui répond en langage naturel aux questions sur la base de vérité, via 3 outils :
  - full_text_search : recherche lexicale sur les fiches (title/body/entity/themes)
  - sql_query        : SELECT en lecture seule (text-to-SQL) sur les colonnes structurées
  - get_document     : corps intégral d'une fiche (context stuffing ciblé)

Prérequis : ANTHROPIC_API_KEY dans l'environnement.
Modèle par défaut : Claude Sonnet 4.6 (MARVEL_MODEL pour basculer, ex. claude-opus-4-8).
"""
import os, sys, json

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import anthropic
import db

MODEL = os.environ.get("MARVEL_MODEL", "claude-sonnet-4-6")

SYSTEM = """You are Marvel OS, an analyst assistant over Marvel Buildings' internal source of truth.

The data: a single SQL table `documents` holding knowledge sheets synthesized from recorded
sales/technical calls (doc_type='call_sheet'). TWO sources feed it, and they are ALL call sheets:
  - source='chorus'       : hand-made call sheets (the original library),
  - source='hubspot_call' : AI call summaries synced from HubSpot.
When you count "call sheets", count ALL rows across both sources unless the user asks for one
source specifically. Columns you can query:
  source_id (unique id = folder name for chorus, HubSpot call id for hubspot_call),
  title, date (YYYY-MM-DD), entity (client/company),
  brand ('modular' = permanent modular kitchens / ContekPro ; 'mobile' = rental kitchen trailers ;
  'multi' = both brands ; 'marvel' = non-kitchen modular), status, themes (';'-separated tags),
  body (full sheet text).

BRAND RULE (important): 'modular' and 'mobile' are two DIFFERENT businesses — permanent
prefab kitchens vs. rental kitchen trailers. NEVER blend them into one undifferentiated list
or number. When an answer spans both, ALWAYS split it by brand (separate sections/columns)
so the reader never confuses the two. If the question targets a single brand, filter to it
(`WHERE brand = ...`) and ignore the others. A per-request scope constraint, when present,
overrides this and restricts everything to that one brand.

For anything theme-related (group by theme, filter by theme, theme x brand, theme over time)
use the NORMALIZED table `document_themes(document_id, theme)` — one row per theme per document —
JOINed to documents on documents.id = document_themes.document_id. Do NOT split the raw themes text
column yourself. Theme tags are a fixed canonical English vocabulary, e.g. 'client relations',
'design', 'codes/permits', 'installation', 'pricing', 'ventilation/hoods', 'transportation/delivery',
'electrical', 'plumbing', 'strategy', 'marketing/prospecting', 'production', 'materials',
'refrigeration', 'gas', 'suppliers', 'maintenance', 'recruitment/team', 'legal/contracts',
'after-sales service', 'documentation/production'.

Two more tables hold HubSpot CRM data (synced from HubSpot), also queryable with sql_query:
  - deals(hs_id, dealname, amount, brand, pipeline, stage, closedate, createdate, owner_id):
    the sales pipeline. `amount` = deal value (numeric, USD). `stage` = resolved stage label.
    deals.brand is a RELIABLE signal: it comes from the deal's product type (HubSpot
    `type_of_product`). A deal whose product is a Trailer (Mobile Kitchen Rental) = 'mobile';
    everything else (Kitchen/Restroom/Other, or none) = 'modular'. A deal that is both a kitchen
    and a trailer counts as 'mobile' (it contains a trailer). The `pipeline` column does NOT
    distinguish brands (all deals live in one "Sales" pipeline) — use brand, not pipeline, for it.
  - contacts(hs_id, email, firstname, lastname, company, jobtitle, lifecyclestage, createdate):
    people in the CRM. No brand column.
Use these for CRM questions (pipeline value, deals by stage, contacts by company/title/lifecycle).
They are SEPARATE from `documents`; there is no clean key to join a call sheet to a deal yet, so
answer call-content questions from `documents` and CRM questions from `deals`/`contacts` — and say
which you used. When a brand scope is active, filter `deals` by its brand column where meaningful;
`contacts` has no brand, so note that it spans both businesses.

Your tools:
  - sql_query(sql): read-only SELECT for structured/aggregate questions (counts, group by, filters
    on brand/date/entity/themes). This is exact and auditable — PREFER it for "how many", "which",
    "by brand", "over time" questions.
  - full_text_search(query, brand?, date_from?, date_to?, entity?): lexical search over the sheets
    for content/topic questions ("calls mentioning X", "objections about Y").
  - get_document(source_id): the full sheet, to read details and quote accurately.

Method: decide which tool fits. For content questions, search then open the most relevant sheets
with get_document before answering. ALWAYS cite the source_id(s) you used, so answers are verifiable.
Answer in the user's language. Be concise and decision-oriented — surface the pattern or the answer,
not a data dump. If the data doesn't support a claim, say so.
"""

TOOLS = [
    {
        "name": "sql_query",
        "description": "Run a read-only SELECT/WITH query. Tables: `documents` (call knowledge sheets), "
                       "`document_themes(document_id, theme)`, and HubSpot `deals` and `contacts`. "
                       "Use for counts, group-by, and exact filters. Returns columns + rows.",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single SELECT or WITH query."}},
            "required": ["sql"],
        },
    },
    {
        "name": "full_text_search",
        "description": "Lexical full-text search over the knowledge sheets (title/body/entity/themes). "
                       "Use for content/topic questions. Optional filters narrow by column.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "brand": {"type": "string", "enum": ["modular", "mobile", "multi", "marvel"]},
                "entity": {"type": "string"},
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_document",
        "description": "Return the full text of one knowledge sheet by its source_id.",
        "input_schema": {
            "type": "object",
            "properties": {"source_id": {"type": "string"}},
            "required": ["source_id"],
        },
    },
]


def run_tool(name, inp):
    try:
        if name == "sql_query":
            return json.dumps(db.run_select(inp["sql"]), ensure_ascii=False, default=str)
        if name == "full_text_search":
            kwargs = {k: v for k, v in inp.items() if k != "query"}
            return json.dumps(db.full_text_search(inp["query"], **kwargs), ensure_ascii=False)
        if name == "get_document":
            doc = db.get_document(inp["source_id"])
            return json.dumps(doc, ensure_ascii=False) if doc else "null"
        return f"Unknown tool: {name}"
    except Exception as e:
        return json.dumps({"error": str(e)})


def make_client():
    """Client Anthropic ; ajoute l'en-tête workspace si ANTHROPIC_WORKSPACE_ID est défini
    (nécessaire pour les clés user/org non scoped à un workspace)."""
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    if ws:
        return anthropic.Anthropic(default_headers={"anthropic-workspace-id": ws})
    return anthropic.Anthropic()


BRANDS = {"modular": "permanent modular kitchens (ContekPro)",
          "mobile": "rental kitchen trailers", "marvel": "non-kitchen modular"}


def _scope_system(scope_brand):
    """Suffixe système qui verrouille l'agent sur UNE marque (None = toutes, séparées)."""
    if not scope_brand:
        return SYSTEM
    label = BRANDS.get(scope_brand, scope_brand)
    return SYSTEM + (
        f"\n\nSCOPE CONSTRAINT — ACTIVE: The user works within ONE brand only: "
        f"'{scope_brand}' ({label}). Restrict EVERY query, count and statement to "
        f"brand = '{scope_brand}'. Do NOT mention, include or compare the other brands. "
        f"You MAY use brand='multi' rows only if directly relevant, and label them as "
        f"involving both brands. If a question is generic, answer it for '{scope_brand}' alone."
    )


def ask(question, client=None, verbose=True, on_tool=None, history=None, scope_brand=None):
    """Pose une question à l'agent ; retourne le texte final. Boucle tool-use manuelle.
    on_tool(name, input) : callback optionnel appelé à chaque appel d'outil (pour l'UI).
    history : liste de messages antérieurs (chat multi-tour) ; None = question isolée.
    scope_brand : 'modular' | 'mobile' | ... pour verrouiller l'agent sur une seule marque."""
    client = client or make_client()
    system = _scope_system(scope_brand)
    messages = list(history or []) + [{"role": "user", "content": question}]
    while True:
        resp = client.messages.create(
            model=MODEL, max_tokens=4000,
            thinking={"type": "adaptive"}, output_config={"effort": "medium"},
            system=system, tools=TOOLS, messages=messages,
        )
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text")
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                if on_tool:
                    on_tool(b.name, b.input)
                if verbose:
                    print(f"    · {b.name}({json.dumps(b.input, ensure_ascii=False)[:90]})")
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": run_tool(b.name, b.input)})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("[!] Définis ANTHROPIC_API_KEY pour lancer l'agent. La couche données marche sans.")
        sys.exit(1)
    q = " ".join(sys.argv[1:]) or "Combien de calls par marque, et quels sont les 3 thèmes les plus fréquents ?"
    print(f"Q: {q}\n")
    print(ask(q))
