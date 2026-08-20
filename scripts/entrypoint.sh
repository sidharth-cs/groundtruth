#!/usr/bin/env bash
# Container entrypoint.
#
# Waits for Postgres, seeds the synthetic corpora, then does whatever it was
# asked to do. Waiting here rather than relying on compose ordering alone
# matters: a healthcheck says the server is accepting connections, not that
# our database and extension exist yet.

set -euo pipefail
cd /app

wait_for_db() {
  local tries=60
  until pg_isready -h db -U doctask -d doctask >/dev/null 2>&1; do
    tries=$((tries - 1))
    if [ "$tries" -le 0 ]; then
      echo "database did not become ready in time" >&2
      exit 1
    fi
    sleep 1
  done
}

echo "waiting for postgres..."
wait_for_db
echo "postgres ready"

# Corpora are generated rather than committed as fixtures alone, because the
# screening dates are relative: regenerating is what keeps the clean control
# pile genuinely clean however long after this was written it is run.
python scripts/make_corpora.py >/dev/null
echo "corpora seeded"

case "${1:-verify}" in
  verify)  exec ./scripts/verify_all.sh ;;
  test)    exec python -m pytest ;;
  mcp)     exec python -m app.mcp.server ;;
  web)     echo "review interface on http://localhost:8000"
           exec python -m uvicorn app.api.main:app --host 0.0.0.0 --port 8000 ;;
  shell)   exec /bin/bash ;;
  *)       exec "$@" ;;
esac
