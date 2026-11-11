# DeepResearch dashboard

React, TypeScript and Vite frontend for the existing FastAPI service.

```powershell
# Run inside frontend/ with the backend listening on port 8000.
npm.cmd ci
npm.cmd run dev
# http://127.0.0.1:5173

npm.cmd test
npm.cmd run build
```

For built serving, restart FastAPI from the repository root and open port 8000.
For an interactive offline demo after building:

```powershell
cd ..
.\.venv\Scripts\python.exe scripts/local_demo.py --serve --port 8001
```

Provider keys belong only in the Python `.env`. Frontend settings contain only
the optional `API_PROXY_TARGET` server-side development proxy address.
See [the dashboard guide](../docs/dashboard.md) for setup, contracts, safety,
tests, and the main files.
