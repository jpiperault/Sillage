# syntax=docker/dockerfile:1
# --- 1. Interface web (construite sur la plateforme de l'hôte : le résultat est du JS/CSS statique)
FROM --platform=$BUILDPLATFORM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- 2. Dépendances Python dans un venv
FROM python:3.12-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /venv
COPY server/requirements.txt /tmp/requirements.txt
RUN /venv/bin/pip install -r /tmp/requirements.txt

# --- 3. Image finale
FROM python:3.12-slim
ENV PATH=/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/server \
    WEB_DIST=/app/web/dist \
    PORT=8080
COPY --from=deps /venv /venv
COPY server/sillage /app/server/sillage
COPY --from=web /web/dist /app/web/dist
RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001
WORKDIR /app
EXPOSE 8080
CMD ["python", "-m", "sillage"]
