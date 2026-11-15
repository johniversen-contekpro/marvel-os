# Marvel OS — Point of View

*One central source of truth for Marvel Buildings' data, with AI agents on top — not a pile of brittle workflows.*

## The idea (your framing, confirmed)

Marvel runs on a dozen tools — Chorus.ai, HubSpot, Google Drive, Gmail, Google Ads, and more. The data lives in silos we don't own and can't query together. **Marvel OS** fixes that: pull every tool into one store **we** control, and put AI agents on top that answer questions and take action — without building a million point-to-point automations.

Two goals, stated plainly:
1. **Own our data** — a canonical store we control and can export, not a dependency on someone else's index.
2. **Agents, not workflows** — a reasoning layer over clean data, instead of hundreds of fragile Zapier/n8n flows.

## The architecture (4 layers)

```
  Ingestion         →   Source of truth      →   Access            →   Agents
  (connectors)          (Postgres, owned)        (SQL + full-text)     (tool-using AI)
  Chorus, HubSpot,      canonical schema,        exact, auditable,     ask in plain
  Drive, Gmail, Ads     entity resolution        fresh on write        language; act
```

- **Ingestion** — one connector per tool, syncing into the store. This is the real work, and it's needed no matter what sits on top.
- **Source of truth** — a single relational database (Postgres). This *is* "own our data": one `pg_dump` and we walk away with everything. Entity resolution lives here (the same client across Chorus, HubSpot, Gmail becomes one entity).
- **Access** — SQL + full-text search. Exact, auditable, and fresh the instant data lands. Covers the vast majority of real questions.
- **Agents** — Claude with a few tools (query SQL, search text, open a document). The agent decides which to use; no hardcoded flow per question. This is what kills the "million workflows."

## On RAG — an option, not the foundation

A vector database (RAG) is **not** a source of truth — it's a derived, lossy index, tied to an embedding model, that you rebuild from the real data. Building Marvel OS *on* RAG would undercut both goals: it doesn't give us ownership, and each source becomes an embedding pipeline to maintain (the "million workflows" in disguise). For structured questions ("how many deals closed this quarter", "which accounts asked about X") it's also imprecise and unverifiable, where SQL is exact.

So: **build the database first; keep semantic search in reserve.** When real usage proves that keyword search misses something (synonyms, paraphrase), we add `pgvector` *inside the same Postgres* as hybrid search — a one-line extension, not a re-architecture. We don't pay that complexity until a real need earns it.

*(This was stress-tested by a 5-model advisory panel — unanimous: Postgres + tool-using agents as the foundation, RAG deferred.)*

## Proof — a working POC, today

We already have the first source live. The Chorus call library — **327 call summaries, deduped and tagged by brand (253 Modular, 51 Mobile, 18 multi, 5 Marvel)** — is loaded into exactly this architecture: one table, full-text + SQL, an agent on top. Ask it in plain language and it answers with citations back to the source call. It runs on a laptop today; the same code points at production Postgres by changing one connection string.

**This is Marvel OS in miniature, on one source.** The demo answers questions like:
- "Which objections recur on permitting?" → sales battlecards
- "Which calls mention data centers / workforce camps?" → an emerging vertical to target
- "Top equipment brands across our calls?" → procurement standardization

### Retrieve, but above all *quantify*

Every call is tagged against a clean, canonical set of ~24 themes (design, codes/permits, pricing, electrical, after-sales service…), normalized into a dedicated table so a theme is a first-class dimension, on par with brand. That buys two different powers, and the second is the one nobody has today:

- **Retrieve** — "show me the Mobile calls touching gas." Useful, but keyword search does much of this already.
- **Quantify & cross** — "what are our 5 dominant topics?", "what separates Mobile from Modular?", "is permitting trending up over time?" Full-text can't do this; structured themes can. The data already shows the signature: *production / materials / strategy* are almost entirely Modular (permanent builds), while *maintenance / after-sales service* weigh far heavier on Mobile (rentals = an ongoing relationship).

> Full-text **finds documents. Themes produce numbers and trends.** That is the shift from a searchable archive to a decision system — answering not just "show me X" but "what do our data say about X, and how does it break down."

The value isn't the archive — it's the **decisions**: where to sell, what to say, what to build, and capturing the founder's know-how so it scales without him on every call.

## Build vs buy

| Buy / adopt | Build |
|---|---|
| Managed Postgres (hosting) | The agent + tool layer (our logic) |
| Off-the-shelf connectors (e.g. Fivetran/Airbyte) where they exist | Entity resolution + the canonical schema (Marvel-specific) |
| Claude API (the reasoning) | The handful of ingestion syncs for tools without a connector |

We don't reinvent databases or LLMs. We build the thin, Marvel-specific layer that turns our data into decisions.

## Roadmap (incremental, value each step)

1. **Now** — POC live on Chorus (done). Demo it.
2. **+2 weeks** — add HubSpot (deals/contacts); first cross-source questions (calls × pipeline). A HubSpot sync already exists to build on.
3. **Per iteration** — one more source (Gmail, Drive, Ads), each landing in the same store.
4. **When earned** — pgvector hybrid search; agents that *act* (draft follow-ups, flag at-risk deals), not just answer.

## The one line

> **RAG isn't the Marvel OS — the database is.** We build the source of truth first, put tool-using agents on top, and keep semantic search in reserve for the day a real need shows up — without ever leaving Postgres.
