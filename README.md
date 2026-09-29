# IncidentPilot

IncidentPilot is an incident-response agent for engineering and DevOps teams. Hindsight provides its agent memory through Retain, Recall, and Reflect. SQLite remains the local metadata store for incident records and workflow state.

## Requirements

- Python 3.11 or newer
- Node.js 20 or newer and npm

## Local setup

### 1. Configure the backend

From the repository root, create and activate a virtual environment, then install the backend package:

```powershell
cd backend
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item ..\.env.example ..\.env
```

If PowerShell blocks activation, use `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that terminal, then activate again.

### 2. Configure Hindsight

IncidentPilot connects to Hindsight over its HTTP API. Copy `.env.example` to `.env` in the repository root, then set these required variables:

| Variable | Value |
| --- | --- |
| `HINDSIGHT_API_URL` | Hindsight base URL, such as `https://api.hindsight.vectorize.io` or your self-hosted server URL (for example `http://localhost:8888`) |
| `HINDSIGHT_API_KEY` | Hindsight API key; keep it private and do not commit `.env` |
| `HINDSIGHT_BANK_ID` | Existing Hindsight memory bank ID for IncidentPilot |
| `OPENAI_API_KEY` | OpenAI API key used by the backend to generate chat replies; keep it private and do not commit `.env` |
| `OPENAI_MODEL` | Optional Responses API model name; defaults to `gpt-6-astra` |

For Hindsight Cloud, create an API key in the Hindsight console and create or select a bank. The key is sent as a Bearer token by the backend and is never exposed to the browser. The API URL should be the server base URL, without an API path suffix.

The Hindsight endpoints are available under `/api/memory`:

- `POST /api/memory/retain` — retain an item, optionally with context, timestamp, and document ID.
- `POST /api/memory/recall` — retrieve matching memories for a query.
- `POST /api/memory/reflect` — ask Hindsight to reason over the bank and return its answer with supporting facts.

Chat messages go to `POST /api/chat`. The backend recalls relevant Hindsight memories, generates the conversational response with the OpenAI Responses API, and retains durable user-provided facts after generation. The OpenAI key stays on the backend and is never sent to the browser.

These endpoints return a clear `503` response when required Hindsight configuration is missing and a sanitized gateway error if Hindsight is unreachable or rejects a request.

### 3. Start the API

In the same `backend` directory and activated environment:

```powershell
uvicorn app.main:app --reload
```

The API is available at `http://127.0.0.1:8000`; interactive API docs are at `http://127.0.0.1:8000/docs`. The SQLite database is created as `backend/incidentpilot.db` on first startup.

### 4. Start the frontend

In a second terminal from the repository root:

```powershell
cd frontend
npm install
npm run dev
```

Open the URL printed by Vite (normally `http://localhost:5173`). The development server proxies `/api` requests to FastAPI.

## Foundation endpoints

- `GET /api/health` — API and database readiness.
- `GET /api/incidents` — list incident metadata records.
- `POST /api/incidents` — create an incident metadata record.

Hindsight is the agent's memory system; SQLite is used only for local application metadata. The incident dataset and incident-response workflow are developed in later phases.
