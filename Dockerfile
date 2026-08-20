# The review interface is built here rather than committed, so a reviewer never
# needs Node installed and `ui/dist` never goes stale against the source.
FROM node:22-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

# Python 3.11 rather than 3.13/3.14: langgraph, psycopg and pgvector all have
# reliable wheels here, and a reviewer should not be waiting on a source build
# the first time they run this.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# postgresql-client gives us pg_isready for the startup wait.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Requirements first so a code change does not reinstall the world.
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

COPY . .

# Outside /app deliberately. Compose bind-mounts the repo over /app so host
# edits are picked up without a rebuild, and that mount would hide anything
# written to /app/ui/dist — which is also gitignored, so the mount would shadow
# it with nothing.
COPY --from=ui /ui/dist /opt/ui

RUN chmod +x scripts/verify_all.sh scripts/entrypoint.sh

# No API key is baked in and none is needed: the offline adapter is the
# default and the whole test suite runs without one.
ENV LLM_PROVIDER=offline \
    DATABASE_URL=postgresql://doctask:doctask@db:5432/doctask \
    DOCTASK_UI_DIR=/opt/ui

ENTRYPOINT ["scripts/entrypoint.sh"]
CMD ["verify"]
