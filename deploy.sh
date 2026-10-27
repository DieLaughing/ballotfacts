#!/usr/bin/env bash
# Build Ballot Facts on a server over SSH and (re)start its Docker Compose service.
#
# Only committed code is deployed, and the site's footer links to that commit, so anyone can read
# exactly what's running. Uncommitted changes stop the deploy.
#
# Settings come from deploy.env next to this script (not committed), or the file named by DEPLOY_ENV
# (e.g. DEPLOY_ENV=deploy.dev.env ./deploy.sh for the staging copy):
#   HOST         ssh host
#   COMPOSE_DIR  folder on that host with a compose.yaml defining SERVICE
#   SERVICE      compose service to recreate (default ballotfacts)
#   REMOTE_DIR   where the source goes on that host (default ballotfacts)
#   TAG          image tag (default latest)
set -euo pipefail
cd "$(dirname "$0")"
ENV=${DEPLOY_ENV:-deploy.env}
[ -f "$ENV" ] && . "./$ENV"
: "${HOST:?set HOST}" "${COMPOSE_DIR:?set COMPOSE_DIR}"
SERVICE=${SERVICE:-ballotfacts}
REMOTE_DIR=${REMOTE_DIR:-ballotfacts}
TAG=${TAG:-latest}

if [ -n "$(git status --porcelain)" ]; then
  echo "Uncommitted changes: commit (and push) first, so the live site matches public code." >&2
  exit 1
fi
COMMIT=$(git rev-parse HEAD)
git branch -r --contains "$COMMIT" | grep -q . || echo "warning: $COMMIT isn't pushed yet; its footer link won't work until it is" >&2

rsync -a --delete --exclude .venv --exclude .git --exclude /data --exclude __pycache__ --exclude .pytest_cache \
  --exclude 'deploy*.env' --exclude '*.local.md' ./ "$HOST:$REMOTE_DIR/"

ssh "$HOST" REMOTE_DIR="$REMOTE_DIR" SERVICE="$SERVICE" TAG="$TAG" COMPOSE_DIR="$COMPOSE_DIR" COMMIT="$COMMIT" bash -s <<'REMOTE'
set -euo pipefail
cd "$REMOTE_DIR"
docker build -q --build-arg BF_COMMIT="$COMMIT" -t "ballotfacts:$TAG" . >/dev/null
docker compose --project-directory "$COMPOSE_DIR" up -d --no-deps --force-recreate "$SERVICE" 2>&1 | tail -1
for _ in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$SERVICE" 2>/dev/null)" = healthy ] && break
  sleep 2
done
docker image prune -f >/dev/null
echo "$SERVICE: $(docker ps --filter name=^$SERVICE\$ --format '{{.Status}}') at ${COMMIT:0:7}"
REMOTE
