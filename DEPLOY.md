# Deploy Marvel OS (Streamlit Community Cloud)

A private demo URL your bosses can open anytime — no dependency on your laptop.

**Architecture:** this **public** repo holds only *code* (no data, no secrets). The call
library lives in a **private Neon Postgres** database. The app is gated by a **password**.
So: the code is public, the data is private, and only people with the password can use the app.

## 0. One-time accounts
- **GitHub** (the repo is already here).
- **Streamlit** — sign in with GitHub at <https://share.streamlit.io>.
- **Neon** — the database (already set up).

## 1. Deploy on Streamlit
1. <https://share.streamlit.io> → **Create app** → **Deploy a public app from GitHub**.
2. Repository: `johniversen-contekpro/marvel-os` · Branch: `main` · Main file: `app.py`.
3. **Deploy** (first build ~2 min).

## 2. Add the secrets
App page → **⋮ → Settings → Secrets**, paste (TOML). Get `DATABASE_URL` from your Neon
dashboard (**use the pooled connection string**); never paste it anywhere public.

```toml
DATABASE_URL = "postgresql://...neon.tech/neondb?sslmode=require"   # pooled, from Neon
ANTHROPIC_API_KEY = "sk-ant-..."         # a clean key from console.anthropic.com
ANTHROPIC_WORKSPACE_ID = "wrkspc_..."    # your Anthropic workspace id
APP_PASSWORD = "..."                     # the password you hand to Yassine
```

Save → the app restarts. Secrets stay on Streamlit's side, never in the repo.

## 3. Share it
Share the app URL **+ the password** with Yassine & co. Anyone without the password sees only
a login screen. It runs 24/7; your laptop can be off.

---

## Security model (how the data stays safe)
- **Code public, data private.** The repo contains zero client data and zero secrets. The call
  sheets live in Neon; reaching them requires the `DATABASE_URL` (a secret, not in the code).
- **Two keys guard the data:** the Neon connection string (full access — you only; rotate in one
  click in Neon if it ever leaks) and the `APP_PASSWORD` (lets someone *query* via the app —
  give only to trusted people; change it anytime in Secrets).
- **Clean history.** `marvel_os.db` is never committed (it's gitignored); the data has never
  been in this repo's git history.

## Things to know
- **Conversation history is durable.** Chats are stored in Postgres (the `chats` table in Neon),
  so they survive reboots/redeploys and follow the app everywhere. (The old on-disk `chats/`
  storage is gone.)
- **API key.** A clean `sk-ant-api...` key is ideal, but a `sk-ant-usr-...` key also works as long
  as `ANTHROPIC_WORKSPACE_ID` is set (the app sends the workspace header).

## Re-ingest / update data (from your machine, points at Neon)
`.env.local` (written by `neon deploy`, gitignored) holds your connection strings locally.
```powershell
python ingest_chorus.py   # idempotent upsert into Neon; add a fiche, re-run, no duplicates
```
