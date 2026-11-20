# AIVOYA Acquisition Fabric — Public Release Acceptance

## Repository scope

This public repository includes source code and a **three-source disabled example registry**.
The original curated 77-source registry and strategic intelligence documents are not published.
Import private registries from `data/private/` (gitignored) using `tools/import_sources.py --file`.

## Local verified and remaining gates

- Python worker unit tests and Go core tests can be exercised offline.
- PostgreSQL 16 full integration, `pgx` dependency resolution, and any real website tests have NOT been verified by the sandbox in this repository.
- `.github/workflows/verification.yml` runs Go + PG16 and offline Python tests on push; live requests only via manual dispatch.
- Source approvals, legal rights, robots/TOS compliance, and browser egress controls are required before production.
- Do not claim release readiness until cloud CI has actually passed and logs have been reviewed.

See [`CLOUD_VERIFICATION.md`](CLOUD_VERIFICATION.md) and [`SECURITY.md`](SECURITY.md).
