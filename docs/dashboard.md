# React research dashboard

React replaces the Streamlit frontend. The existing FastAPI API, separate durable
worker, LangGraph workflow, CLI, and provider settings remain in place.
The current backend uses normalized PostgreSQL storage and email/password login;
see the [database and accounts guide](auth_postgres.md). No live provider calls are needed to build or test the frontend.

## Development

Use Python 3.11+ and Node 22.13+ on the 22.x line, or Node 24+. The checked local
environment has Python 3.13 and Node 24.13. `npm.cmd` works in PowerShell without
changing the execution policy.

From the repository root, install the Python project and frontend dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
cd frontend
npm.cmd ci
cd ..
```

Start PostgreSQL and apply migrations first:

```powershell
.\.venv\Scripts\python.exe scripts/postgres_local.py start
.\.venv\Scripts\python.exe -m deepresearch_ai.db upgrade
```

Start three terminals from the repository root. Set `MOCK_MODE=true` in both
Python terminals for credential-free development. Stop an existing worker before
replacing it: two workers with different modes should not share the same queue.

```powershell
# Terminal 1
$env:MOCK_MODE = "true"
.\.venv\Scripts\python.exe -m uvicorn deepresearch_ai.api.app:create_app --factory --host 127.0.0.1 --port 8000

# Terminal 2
$env:MOCK_MODE = "true"
.\.venv\Scripts\python.exe -m deepresearch_ai.worker

# Terminal 3
cd frontend
npm.cmd run dev
```

Open http://127.0.0.1:5173 and create an account or sign in. For real research, remove the `MOCK_MODE` override,
configure provider keys only in the Python `.env`, and restart the worker.
Provider quotas and evidence requirements apply to real runs.

## Build and serve from FastAPI

```powershell
cd frontend
npm.cmd run build
cd ..
.\.venv\Scripts\python.exe -m uvicorn deepresearch_ai.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

FastAPI serves the build at http://127.0.0.1:8000 when
`FRONTEND_DIST_DIR` (default `frontend/dist`) contains `index.html`. Restart the
API after the first build. API routes retain priority over static files. Hash
navigation (`/#run/<id>` and `/#history`) supports bookmarking and browser history
without a catch-all route obscuring API errors. The worker still runs separately.
Build files are not embedded into Python wheels; ship `frontend/dist` alongside
the backend and set an absolute `FRONTEND_DIST_DIR` if running elsewhere.

During development, Vite proxies `/auth`, `/research` and `/health` to port 8000. Override
the development target in `frontend/.env` with `API_PROXY_TARGET` if needed.
The browser always uses relative same-origin URLs; no permissive CORS setting is
required. The frontend `.env` is separate from the repository's Python `.env`.
Never add provider credentials or `VITE_` API keys to the frontend.

## Offline interactive demo

After the frontend build, run this from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts/local_demo.py --serve --port 8001
```

Open http://127.0.0.1:8001. It starts a separate mock backend and worker, completes
an initial example using a temporary account. Create your own temporary account
in the dashboard to try more research; the initial account remains private. It uses a
temporary SQLite database, strips provider credentials from child environments,
and preserves the normal `.env` and research history. Press Ctrl+C to stop and
remove the temporary database. Omit `--serve` for a one-shot HTTP smoke check
that cleans up automatically and writes `demo_report.md`.

## What the dashboard does

- Registers/signs in users, restores sessions, and signs out of the private workspace.
- Creates research with a topic, optional instructions, and Quick/Standard/Deep depth.
- Shows each agent's status, current round, and estimated progress; active runs
  poll sequentially every two seconds, with stale requests aborted on navigation.
- Lists saved runs with pagination and a clearly labeled filter of the current page.
- Displays reports, source links/dates/IDs, claim verdicts, and exact evidence passages.
- Distinguishes retrieved-page counts from validated passages and supported claims.
- Preserves insufficient-evidence and provider failures instead of displaying a
  successful answer when evidence checks failed.
- Cancels queued/running jobs through the existing API and downloads the existing
  server-sanitized Markdown export.
- Supports mobile navigation, keyboard-operated tabs, visible focus indicators,
  and reduced-motion preferences.

Reports use `react-markdown` with raw HTML skipped and images excluded. Markdown
links must match a recorded, safe source URL. Source links conservatively reject
local names, IP literals, credentials, nonstandard ports, and non-HTTP(S) schemes.
No report HTML is inserted through `dangerouslySetInnerHTML`. URL syntax checks
do not establish source credibility or guarantee future destination behavior;
retrieval's DNS/private-address protections remain in Python.

## Main files

| File | Responsibility |
| --- | --- |
| `frontend/src/App.tsx` | Navigation, history sidebar, backend connectivity |
| `frontend/src/components/ResearchForm.tsx` | Topic, instructions, depth, submission |
| `frontend/src/components/RunWorkspace.tsx` | Active run, counts, agent steps, cancellation |
| `frontend/src/components/ReportView.tsx` | Markdown, sources, claims, sanitized download |
| `frontend/src/components/HistoryView.tsx` | Paginated history and page filtering |
| `frontend/src/api.ts`, `types.ts` | Existing API calls and public TypeScript contracts |
| `frontend/src/hooks.ts` | Sequential polling and abort/cleanup behavior |
| `frontend/src/safety.ts` | Conservative source-link syntax checks |
| `frontend/src/styles.css` | Responsive layout, reading surface, interaction styles |
| `frontend/vite.config.ts` | Local proxy and frontend test configuration |

React uses local component state and a small polling hook. No global state
framework or replacement job mechanism is needed. Authentication uses the backend
HttpOnly session cookie and an in-memory CSRF token; private views are unmounted
on sign-out or session expiration.
Current API usage was checked against [React effects](https://react.dev/reference/react/useEffect),
[Vite](https://vite.dev/guide/), [Vite proxy configuration](https://vite.dev/config/server-options.html#server-proxy),
[react-markdown](https://github.com/remarkjs/react-markdown), and [Vitest](https://vitest.dev/guide/).

## Validation

```powershell
.\.venv\Scripts\python.exe -m pytest -q
cd frontend
npm.cmd test
npm.cmd run build
```

The updated Python suite has **475 passing tests** (plus 3 separately passing PostgreSQL integration tests). The **38 passing React
tests** cover API errors, request data, validation, suggestions, queued-to-completed
polling, cancellation, history, evidence, downloads, keyboard tabs, and unsafe
content/URLs. Automated tests mock HTTP and never call Gemini or Tavily.
Streamlit-specific cases were replaced with React flows; backend sanitization
checks remain, with new API/static-file isolation tests.

TypeScript checking and the production Vite build pass. The isolated real HTTP
demo verifies report generation, two sources/citations, downloads, built HTML,
and bundled assets. Computer-use visual verification stopped because the tool
could not reliably determine the current Chrome URL; no visual browser pass is
claimed.
