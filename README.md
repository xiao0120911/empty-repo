# Regulatory Agent V2

Market-supervision workflow assistant built with FastAPI, Vue 3, LangGraph, SQLite, an OpenAI-compatible model endpoint, and an allow-listed upstream platform adapter.

## Capabilities

- Natural-language planning with allow-listed platform tools and structured audit events.
- Human approval gate for approval and disposition decisions.
- Image upload, video-channel snapshots, and two visual inspection scenes.
- Persistent periodic video snapshot monitoring with deduplicated internal alerts.
- Hash-chain audit verification and encrypted server-side session evidence.

## Local development

1. Copy `.env.example` to `.env` and set `FERNET_KEY` plus the required model and upstream settings.
2. Install backend dependencies from `backend/requirements.txt` and frontend dependencies with `npm ci` in `frontend`.
3. Create a local user with `python -m app.manage create-user --account <account> --role operator` from `backend`.
4. Start the backend with `python -m uvicorn main:app --host 127.0.0.1 --port 8000` from `backend`.
5. Start the frontend with `npm run dev` from `frontend`.

The application API documentation is available at `/api/docs` through the backend entry point.

## Production

Use `.env.production.example` as the production configuration template. Production startup rejects mock upstream configuration, HTTP public URLs, insecure cookies, missing Fernet keys, and disabled password policy.

Run the production compose overlay with a real `.env.production` file and TLS certificate files in `deploy/tls/`. Never commit `.env`, `.env.production`, TLS keys, runtime data, or upstream/model credentials.

## Data assets

The local image collections are deliberately excluded from this repository. Use approved object storage or Git LFS only when dataset distribution is authorized.
