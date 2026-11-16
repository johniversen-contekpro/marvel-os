#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Marvel OS — interface de chat (démo CEO), avec conversations persistées.

Fine couche Streamlit par-dessus l'agent outillé (marvel_agent.ask). Montre :
  - une conversation en langage naturel, réponses formatées (tableaux, gras) ;
  - les outils / sources que l'agent a utilisés (argument "auditable, sourcé") ;
  - un sélecteur de MARQUE (Modular / Mobile) pour ne jamais mélanger les deux business ;
  - des questions pré-remplies cliquables ;
  - les CONVERSATIONS sauvegardées sur disque (chats/*.json), rechargeables à tout moment.

Lancer :  streamlit run app.py
Prérequis : une clé Anthropic (barre latérale ou ANTHROPIC_API_KEY).
"""
import os
import json
from datetime import datetime
from pathlib import Path

import re
import streamlit as st

# Config via variables d'environnement, chargée AVANT d'importer db/agent
# (db lit DATABASE_URL au chargement ; l'agent lit ANTHROPIC_API_KEY / WORKSPACE_ID).
_CONFIG_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID", "DATABASE_URL", "APP_PASSWORD")

# (1) Dev local : .env.local (écrit par `neon deploy`, jamais commité).
_envfile = Path(__file__).resolve().parent / ".env.local"
if _envfile.exists():
    for _line in _envfile.read_text(encoding="utf-8").splitlines():
        _m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", _line)
        if _m:
            os.environ.setdefault(_m.group(1), _m.group(2).strip().strip('"').strip("'"))

# (2) Cloud : secrets du dashboard Streamlit.
try:
    for _k in _CONFIG_KEYS:
        _v = st.secrets.get(_k)
        if _v:
            os.environ.setdefault(_k, str(_v))
except Exception:
    pass

import db
import marvel_agent as agent

DEFAULT_WS = ""  # workspace id vient des secrets (ANTHROPIC_WORKSPACE_ID), jamais en dur
CHATS_DIR = Path(__file__).resolve().parent / "chats"
CHATS_DIR.mkdir(exist_ok=True)

SAMPLE_QUESTIONS = [
    "Onboarding cheat sheet for a new rep: the 5 key themes and the recurring objections.",
    "Which objections recur on permitting? Cite 2-3 specific calls.",
    "Which calls mention data centers or workforce camps? Is it an emerging vertical?",
    "Which themes dominate, and what does that reveal about how we sell?",
    "Which equipment brands (ovens, refrigeration, hoods) come up most?",
]

# Marque Marvel — Navy #0A2342 / Rouge #C8102E.
BRAND_CSS = """
<style>
:root { --navy:#0A2342; --red:#C8102E; }
h1, h2, h3 { color: var(--navy); }
a, a:visited { color: var(--red) !important; }
section[data-testid="stSidebar"] { border-right: 3px solid var(--red); }
section[data-testid="stSidebar"] h1 { color: var(--navy); }
.marvel-banner {
  background: linear-gradient(90deg, #0A2342 0%, #163a63 100%);
  border-left: 6px solid var(--red);
  border-radius: 12px; padding: 20px 26px; margin: 2px 0 14px 0;
}
.marvel-wordmark { font-size: 30px; font-weight: 800; letter-spacing: .6px; color:#fff; line-height:1; }
.marvel-wordmark .os { color: var(--red); }
.marvel-sub { color:#C9D6E6; font-size: 13.5px; margin-top: 7px; }
.stButton button:hover { border-color: var(--red) !important; color: var(--red) !important; }
/* Sidebar un peu plus compacte, sans chevauchement */
section[data-testid="stSidebar"] hr { margin: 0.5rem 0; }
section[data-testid="stSidebar"] h1 { font-size: 1.4rem; margin-bottom: .25rem; }
section[data-testid="stSidebar"] h2, section[data-testid="stSidebar"] h3 { font-size: 1.05rem; margin-top: .25rem; }
</style>
"""

BRAND_BANNER = """
<div class="marvel-banner">
  <div class="marvel-wordmark">🏗️&nbsp;MARVEL&nbsp;<span class="os">OS</span></div>
  <div class="marvel-sub">Marvel Buildings — one source of truth, with an AI agent on top</div>
</div>
"""

st.set_page_config(page_title="Marvel OS", page_icon="🏗️", layout="centered")


def _check_password():
    """Barrière par mot de passe (APP_PASSWORD). Non défini = accès libre (dev local)."""
    pw = os.environ.get("APP_PASSWORD")
    if not pw or st.session_state.get("auth_ok"):
        return True
    st.markdown(BRAND_CSS, unsafe_allow_html=True)
    st.markdown(BRAND_BANNER, unsafe_allow_html=True)
    st.caption("Private demo — enter the password to continue.")
    entered = st.text_input("Password", type="password")
    if entered:
        if entered == pw:
            st.session_state.auth_ok = True
            st.rerun()
        else:
            st.error("Wrong password.")
    return False


if not _check_password():
    st.stop()

# Garde : base non configurée (DATABASE_URL absent et pas de SQLite local) -> message clair.
if not db.IS_POSTGRES and not db.DEFAULT_SQLITE.exists():
    st.error(
        "⚠️ **Database not configured.** Add **`DATABASE_URL`** in the app's "
        "**Settings → Secrets** (the Neon *pooled* connection string, in quotes, on one line), "
        "then save — the app reboots and connects to Neon."
    )
    _present = [k for k in _CONFIG_KEYS if os.environ.get(k)]
    st.caption("🔎 Diagnostic — secrets detected (names only): "
               + (", ".join(_present) if _present else "**none** (secrets not saved/loaded)"))
    st.stop()


# --------------------------------------------------------------------------- persistence
def new_chat_id():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def save_chat(chat_id, messages, scope):
    if not messages:
        return
    title = next((m["content"] for m in messages if m["role"] == "user"), "New chat")
    data = {
        "id": chat_id,
        "title": title[:60],
        "scope": scope or "all",
        "updated": datetime.now().isoformat(timespec="seconds"),
        "messages": messages,
    }
    (CHATS_DIR / f"{chat_id}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_chats():
    out = []
    for f in CHATS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append(d)
        except Exception:
            continue
    return sorted(out, key=lambda d: d.get("updated", ""), reverse=True)


def load_chat(chat_id):
    f = CHATS_DIR / f"{chat_id}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8")).get("messages", [])
    return []


def delete_chat(chat_id):
    (CHATS_DIR / f"{chat_id}.json").unlink(missing_ok=True)


@st.cache_data(ttl=300)
def brand_counts():
    rows = db.run_select(
        "SELECT brand, COUNT(*) FROM documents GROUP BY brand ORDER BY 2 DESC"
    )["rows"]
    return rows, sum(n for _, n in rows)


st.markdown(BRAND_CSS, unsafe_allow_html=True)   # charte Marvel, avant le rendu de la sidebar

# --------------------------------------------------------------------------- state
if "chat_id" not in st.session_state:
    st.session_state.chat_id = new_chat_id()
if "messages" not in st.session_state:
    st.session_state.messages = []

# --------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("🏗️ Marvel OS")
    st.caption("One source of truth for Marvel's data, with an AI agent on top.")

    rows, total = brand_counts()
    st.markdown(f"**{total}** call sheets &nbsp;·&nbsp; "
                + " · ".join(f"{b} {n}" for b, n in rows))

    st.subheader("Brand scope")
    SCOPES = {
        "All brands (kept separate)": None,
        "Modular only — permanent kitchens": "modular",
        "Mobile only — rental trailers": "mobile",
    }
    scope_label = st.radio("Scope", list(SCOPES), label_visibility="collapsed")
    scope_brand = SCOPES[scope_label]

    st.divider()
    st.subheader("💬 Conversations")
    if st.button("➕ New chat", use_container_width=True):
        st.session_state.chat_id = new_chat_id()
        st.session_state.messages = []
        st.rerun()

    for d in list_chats():
        active = d["id"] == st.session_state.chat_id
        col1, col2 = st.columns([0.82, 0.18])
        label = ("🟢 " if active else "") + (d["title"] or "Untitled")
        if col1.button(label, key=f"load_{d['id']}", use_container_width=True,
                       help=f"{d['scope']} · {d['updated']}"):
            st.session_state.chat_id = d["id"]
            st.session_state.messages = load_chat(d["id"])
            st.rerun()
        if col2.button("🗑", key=f"del_{d['id']}", use_container_width=True):
            delete_chat(d["id"])
            if active:
                st.session_state.chat_id = new_chat_id()
                st.session_state.messages = []
            st.rerun()

    st.divider()
    st.subheader("Try a question")
    for i, q in enumerate(SAMPLE_QUESTIONS):
        if st.button(q, key=f"sample_{i}", use_container_width=True):
            st.session_state.pending_q = q
            st.rerun()

    st.divider()
    with st.expander("⚙️ Connection", expanded=not os.environ.get("ANTHROPIC_API_KEY")):
        key = st.text_input("Anthropic API key", type="password",
                            value=os.environ.get("ANTHROPIC_API_KEY", ""))
        ws = st.text_input("Workspace ID",
                           value=os.environ.get("ANTHROPIC_WORKSPACE_ID", DEFAULT_WS))
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
        if ws:
            os.environ["ANTHROPIC_WORKSPACE_ID"] = ws
    st.caption(f"Model: `{agent.MODEL}`  ·  No RAG — SQL + full-text + tools.")

# --------------------------------------------------------------------------- header
st.markdown(BRAND_BANNER, unsafe_allow_html=True)
st.caption("Plain-language questions over 327 synthesized sales/technical calls. "
           "Every answer is sourced back to the call it came from.")
if scope_brand:
    st.info(f"🎯 Scope locked to **{scope_brand.capitalize()}** — the agent ignores the other brand.")
else:
    st.caption("Scope: **all brands**, kept separate in answers (never blended).")

TOOL_LABEL = {"sql_query": "SQL query", "full_text_search": "Full-text search",
              "get_document": "Open document"}


def render_tools(tools):
    if not tools:
        return
    with st.expander(f"🔎 {len(tools)} tool call(s) — how this answer was built"):
        for name, inp in tools:
            st.markdown(f"**{TOOL_LABEL.get(name, name)}**")
            st.code(inp.get("sql") or inp.get("query") or inp.get("source_id") or str(inp),
                    language="sql" if name == "sql_query" else None)


# replay history
for m in st.session_state.messages:
    with st.chat_message(m["role"], avatar="🏗️" if m["role"] == "assistant" else None):
        st.markdown(m["content"])
        render_tools(m.get("tools"))

# new input: typed box OR a clicked sample question
prompt = st.chat_input("Ask about our calls…")
if not prompt and "pending_q" in st.session_state:
    prompt = st.session_state.pop("pending_q")

if prompt:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        st.error("Add your Anthropic API key in the sidebar (⚙️ Connection) to run the agent.")
        st.stop()

    history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant", avatar="🏗️"):
        tools = []
        try:
            with st.spinner("Thinking — querying the source of truth…"):
                answer = agent.ask(prompt, verbose=False,
                                   on_tool=lambda n, i: tools.append((n, i)),
                                   history=history, scope_brand=scope_brand)
        except Exception as e:
            answer = f"⚠️ Error: {e}"
        st.markdown(answer)
        render_tools(tools)

    st.session_state.messages.append({"role": "assistant", "content": answer, "tools": tools})
    save_chat(st.session_state.chat_id, st.session_state.messages, scope_brand)
    st.rerun()   # refresh the sidebar list so the new/updated chat appears
