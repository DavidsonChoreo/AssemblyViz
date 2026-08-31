# AssemblyViz — single container serving the SPA and the API from one origin.
#
# The app already assumes this. frontend/vite.config.ts proxies /api to the
# backend in development and notes that "in production the frontend and backend
# share an origin, so relative paths work directly", and API_BASE in App.tsx
# defaults to '' for the same reason. Nothing about the client changes here.

# ---- stage 1: build the SPA -------------------------------------------------
FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- stage 2: runtime -------------------------------------------------------
FROM python:3.12-slim
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
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
