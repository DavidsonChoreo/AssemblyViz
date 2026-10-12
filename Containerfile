# AssemblyViz — single container serving the SPA and the API from one origin.
#
# The app already assumes this. frontend/vite.config.ts proxies /api to the
# backend in development and notes that "in production the frontend and backend
# share an origin, so relative paths work directly", and API_BASE in App.tsx
# defaults to '' for the same reason. Nothing about the client changes here.

# ---- stage 1: build the SPA -------------------------------------------------
FROM node:22-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- stage 2: runtime -------------------------------------------------------
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/
COPY --from=frontend /build/dist ./frontend/dist

# Non-root. Port 8000 is above 1024, so binding it needs no capability.
RUN useradd --create-home --uid 10001 appuser
USER appuser

# WORKDIR is backend/ because main.py imports its siblings by bare name
# ("from hymn.parser import ..."), the same reason conftest.py puts backend/ on
# sys.path for the tests.
WORKDIR /app/backend
EXPOSE 8000

# Calls /health on the app itself. The slim image has no curl, so it uses
# Python's standard library. Container Apps doesn't run this: its probes are
# HTTP or TCP and live in the hosting stack. This one serves the Choreo tier
# check and anyone running the image locally.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
