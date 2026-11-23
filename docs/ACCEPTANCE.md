# AIVOYA Acquisition Fabric — Public Release Acceptance

## Repository scope

This public repository includes source code and a **three-source disabled example registry**.
The original curated 77-source registry and strategic intelligence documents are not published.
Import private registries from `data/private/` (gitignored) using `tools/import_sources.py --file`.

## Local verified and remaining gates

- Python worker unit tests and Go core tests can be exercised offline.
- `go.sum` is committed. Locally, Go 1.23.12 (`golang:1.23-bookworm`, `GOTOOLCHAIN=local`) ran `go mod tidy` (the `go` directive stayed `1.23.0`; no `toolchain` line), then `go mod verify`, `go build ./...`, `go vet ./...`, and `go test -count=1 -timeout=10m -v ./...` against a dedicated empty PostgreSQL 16.15 database (`postgres:16-alpine`). `TestIntegrationLifecycle`, `TestIntegrationV11PolicyRotationAndGitHub`, and `TestIntegrationV11RetryAfterDomainCooldown` passed and were not skipped. This was a local container run, not a GitHub Actions run.
- Real website acquisition, the Docker Compose rehearsal, and the online CI jobs have not been run.
- `.github/workflows/verification.yml` runs Go + PG16 and offline Python tests on push; live requests only via manual dispatch. The Go job runs `go mod tidy -diff` and `go mod verify` and fails when the lockfile is not tidy, instead of resolving modules at build time.
- Source approvals, legal rights, robots/TOS compliance, and browser egress controls are required before production.
- Do not claim release readiness until cloud CI has actually passed and logs have been reviewed.

See [`CLOUD_VERIFICATION.md`](CLOUD_VERIFICATION.md) and [`SECURITY.md`](SECURITY.md).
