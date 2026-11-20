# Security & Production Gate

## Defaults

1. Every imported source is `pending` and `enabled=false`.
2. Jobs are allocated only after source review, rights approval and policy-version match.
3. Worker enforces `robots.txt` (fail closed except 404/410) and blocks private/non-global IPs, disallowed redirects, special ports and unsupported methods.
4. The default engine is Python standard-library HTTP. Scrapling browser render requires explicit opt-in and an enforced egress allowlist; **do not expose browser-enabled workers to unrestricted networks**.
5. The console requires a distinct admin token for data access; workers have separate credentials. Tokens are generated with `secrets.token_urlsafe` and do not belong in Git.
6. Failed or delayed completions cannot update a reclaimed lease because tokens are rotated.
7. Revoked content rights cause immediate content/snapshot deletion, and policy versions prevent pending jobs from continuing.
8. Original snapshots are stored in PostgreSQL BYTEA (max 2 MiB) for a short period: 7 days if rights are restricted, 30 days if licensed. Backups must honor retention and erasure rules too.

## Production P0 residual risks

- Browser-based fetches can create nested resource requests and redirects. The code-level DNS check **does not fully prevent DNS rebinding** or loading arbitrary browser subresources. Use firewall/egress proxy and per-request host interception before enabling Scrapling.
- `AIVOYA_USER_AGENT` must be configured with an actual operator contact approved for use; there is intentionally no fabricated website or contact address in defaults.
- No per-operator RBAC (single admin key), network TLS termination, OAuth2/OIDC, centralized KMS secrets, immutable audit-storage, penetration test or SCA report included yet.
- The basic extractor does not validate factual truth, image rights, author byline, entity linking or upstream content trust. Do not publish ingested full text without separately verified rights.
- V1.1 now adds versioned SQL migrations under a transaction/advisory lock. This code path has not been run against an actual PostgreSQL 16 instance here; DB upgrade and rollback exercise are still release blockers.

## Escalation / Rollback

- Stop worker services: `docker compose stop worker`.
- Review `/v1/jobs`, policy events and `job_events` before restarting.
- Deny source when rights change (`POST /v1/sources/{id}/deny`) and preserve the review reason.
- Reduce crawler scope; do not automatically attempt proxy rotation or CAPTCHA solving.
