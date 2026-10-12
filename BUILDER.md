# Building notes

Notes for anyone, person or agent, working on this app. The builder and Claude keep this file current. The rules in `AGENTS.md` come first when the two disagree.

## What this app does

AssemblyViz lets Computer Organization students step through assembly programs on two architectures, HYMN (8-bit) and RISC-V RV32I (32-bit), and watch registers and memory change at each instruction. A FastAPI backend parses, assembles and simulates; a React frontend built with Vite shows the state. `PASSPORT.yml`'s purpose is the official version.

## Running it locally

Backend, from `backend/`:

```bash
pip install -r requirements-dev.txt
uvicorn main:app --reload
```

Frontend, from `frontend/`:

```bash
npm ci
npm run dev
```

The frontend runs on `http://localhost:5173` and proxies `/api` to the backend on port 8000. Tests run from the repository root with `pytest -q`.

The production image is the `Containerfile` at the root: one container serves the built frontend and the API from the same origin.

## Conventions

- The backend is stateless. Every request carries the full machine state, and the backend rebuilds it from the payload. Keep it that way: the passport says the app writes nothing.
- Each architecture follows the same layers: parser, assembler or executor, simulator, then a JSON snapshot of the state for the frontend.
- Python dependencies are pinned to exact versions in `backend/requirements.txt`. The frontend's `package.json` still uses version ranges, with `package-lock.json` pinning what installs; add new frontend dependencies at exact versions, as `AGENTS.md` asks. Dependabot proposes updates weekly.

## Things to know

- `main.py` imports its siblings by bare name (`from hymn.parser import ...`), so it runs from `backend/`. `conftest.py` puts `backend/` on the path for the tests for the same reason.
- Request bodies are capped at 256 KiB and source text at 64 KiB, because the simulator is CPU-bound and every endpoint takes user input.
- Sign-in is Davidson SSO, enforced by the platform in front of the app. The app has no login code of its own.
- `.github/workflows/deploy.yml` deploys every push to `main` to Azure Container Apps. The hosting itself is defined in `DavidsonCollege/choreo-hosting-monorepo` under `apps/assemblyviz/`.
- Built in spring 2026 by a student team (Nikita Aleksii, Maria Fajardo, Robby Votta and Yurdanur Yolcu). Their reports and poster are in `reports/`.
