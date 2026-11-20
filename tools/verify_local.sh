#!/usr/bin/env bash
# Execute against a disposable PG16 database once dependencies can be downloaded.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHONPATH=worker python -m unittest discover -s worker/tests -v
python -m compileall -q worker tools
go mod download
go test -count=1 ./...
go vet ./...
if [[ ! -s go.sum ]]; then
  echo 'go.sum absent after dependency download; release gate remains open' >&2
  exit 2
fi
go mod verify
if [[ -z "${TEST_DATABASE_URL:-}" ]]; then
  echo 'WARNING: PostgreSQL integration was not run (set TEST_DATABASE_URL)' >&2
else
  go test -count=1 ./internal/store -run Integration -v
fi
