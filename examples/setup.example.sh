#!/usr/bin/env bash
# Repo-specific setup that bin/whooper-review runs after the head and merge-base worktrees exist
# and before the review starts. Copy it next to config.toml and name it in the repo's table.
#
# Whooper sets:
#   WHOOPER_RUN_ID      the run's ID, unique, for naming containers
#   WHOOPER_RUN         the run directory
#   WHOOPER_HEAD        the head worktree
#   WHOOPER_BASE        the merge-base worktree
#   WHOOPER_CHANGED     a file listing the changed paths, one per line
#   WHOOPER_SETUP_ENV   a file to append KEY=value lines to; the review run gets them as variables
# Whatever this prints on stdout goes into the review prompt's facts, so say what the run can use.
# A non-zero exit stops the review. The teardown script runs afterwards either way.
#
# This example starts a PostgreSQL container when a change touches the made-up services/orders.
set -euo pipefail

if ! grep -q '^services/orders/' "$WHOOPER_CHANGED"; then
  echo "No database: the change does not touch services/orders."
  exit 0
fi

name="whooper-$WHOOPER_RUN_ID-postgres"
docker run --detach --name "$name" --publish 127.0.0.1::5432 \
  --env POSTGRES_PASSWORD=local-only --health-cmd 'pg_isready -U postgres' --health-interval 2s \
  postgres:16 >/dev/null
for _ in $(seq 60); do
  [[ "$(docker inspect --format '{{.State.Health.Status}}' "$name")" == healthy ]] && break
  sleep 2
done
port="$(docker port "$name" 5432/tcp | sed -n 's/^127\.0\.0\.1:\([0-9]*\)$/\1/p')"
echo "ORDERS_DATABASE_URL=postgres://postgres:local-only@127.0.0.1:$port/postgres" >> "$WHOOPER_SETUP_ENV"
(cd "$WHOOPER_BASE/services/orders" && ORDERS_DATABASE_URL="postgres://postgres:local-only@127.0.0.1:$port/postgres" make migrate)
echo "PostgreSQL at 127.0.0.1:$port (container $name), migrated at the merge base; ORDERS_DATABASE_URL points at it."
