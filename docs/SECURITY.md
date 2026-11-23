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
9. Stdlib acquisition connects to the address validated for that hop and ignores ambient proxy variables. `AIVOYA_EGRESS_PROXY` (`http://host:port`, no credentials) is the only proxy; when it is set, destination-address enforcement for those fetches is delegated to the proxy. Control-plane calls also bypass proxies. Scrapling is not covered.

## Production P0 residual risks

- Stdlib fetches are pinned to the validated address, and ambient proxy variables are ignored. Evidence is offline unit tests with a simulated resolver; this has not been run against a real hostile DNS server.
- `AIVOYA_EGRESS_PROXY` sends stdlib acquisition through that proxy only. Destination-address enforcement is then delegated to the proxy, because the worker does not resolve the target locally. Startup logs that delegation.
- Browser and Scrapling subresource requests and redirects are still unpinned. The code-level check does not stop those paths from reaching a private address. Use an egress allowlist and per-request host interception before enabling Scrapling.
- The Compose `worker` service is still attached to the unrestricted `public` network. A network-level allowlist or firewall is still required before production.
- `AIVOYA_USER_AGENT` must be configured with an actual operator contact approved for use; there is intentionally no fabricated website or contact address in defaults.
- No per-operator RBAC (single admin key), network TLS termination, OAuth2/OIDC, centralized KMS secrets, immutable audit-storage, penetration test or SCA report included yet.
- The basic extractor does not validate factual truth, image rights, author byline, entity linking or upstream content trust. Do not publish ingested full text without separately verified rights.
- V1.1 now adds versioned SQL migrations under a transaction/advisory lock. This code path has not been run against an actual PostgreSQL 16 instance here; DB upgrade and rollback exercise are still release blockers.

## Escalation / Rollback

- Stop worker services: `docker compose stop worker`.
- Review `/v1/jobs`, policy events and `job_events` before restarting.
- Deny source when rights change (`POST /v1/sources/{id}/deny`) and preserve the review reason.
- Reduce crawler scope; do not automatically attempt proxy rotation or CAPTCHA solving.
