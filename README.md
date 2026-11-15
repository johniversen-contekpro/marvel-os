# Marvel OS — POC

Source-of-truth + tool-using AI agent over Marvel's call data. Source #1 = the Chorus
knowledge sheets (`../yassine-kb`). No RAG — SQL + full-text + an agent. See `MARVEL_OS_POV.md`.

## What's here

| File | Role |
|---|---|
| `db.py` | DB layer (SQLite+FTS5 by default, Postgres via `DATABASE_URL`). Tools: `full_text_search`, `run_select`, `get_document`. |
| `ingest_chorus.py` | Connector #1: loads `yassine-kb/index.csv` + `fiches/*.md` into `documents`. Idempotent. |
| `marvel_agent.py` | The agent (Claude Sonnet 4.6, tool-use loop). |
| `demo.py` | Runs 5-6 real questions through the agent. |
| `schema.sql` | Postgres DDL (for production) + read-only `marvel_ro` role. |

## Run it (nothing to install)

The POC runs on **SQLite + FTS5**, built into Python — zero install.

```sh
cd C:\Users\johni\marvel-os
python ingest_chorus.py            # builds marvel_os.db (327 sheets)
```

The data layer is testable immediately (no API key):

```sh
python -c "import db; [print(r['source_id'], '|', r['title']) for r in db.full_text_search('permitting', brand='modular', limit=3)]"
```

To run the **agent / demo**, set an Anthropic API key (from console.anthropic.com):

```sh
set ANTHROPIC_API_KEY=sk-ant-...          # Windows (cmd)     — or:  $env:ANTHROPIC_API_KEY="sk-ant-..."  (PowerShell)
python demo.py                            # the 5-6 question demo
python marvel_agent.py "How many Mobile calls mention propane?"   # one-off question
```

Switch the agent model (optional): `set MARVEL_MODEL=claude-opus-4-8`.

## Switch to Postgres (production, later)

No code changes — same files, one env var.

```sh
createdb marvel_os
psql -d marvel_os -f schema.sql
set DATABASE_URL=postgresql+psycopg2://marvel:pwd@localhost/marvel_os
python ingest_chorus.py
```

`pgvector` (semantic/hybrid search) is deferred by design — add it inside the same Postgres
only when real queries prove full-text isn't enough (see POV).

## Re-ingest after the sheets change

`ingest_chorus.py` is idempotent — re-run it anytime (e.g. after the English rewrite of the
sheets finishes) to refresh the store. No duplicates.

## Next source (iteration 2)

HubSpot (deals/contacts) → same `documents` table + normalized tables. A sync already exists
at `C:\Users\johni\.config\claude-seo\weekly_hub_sync.py` to build on; MCP `hubspot` is configured.
