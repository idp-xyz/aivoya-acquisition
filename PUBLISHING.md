# Publishing this code repository

Target: `idpxyz/aivoya-acquisition` (PUBLIC, branch `main`).

## Windows / Mac / Linux, using GitHub CLI

1. Install and sign into [GitHub CLI](https://cli.github.com/): `gh auth login`.
2. Open a terminal in this project directory.
3. Execute:

```sh
git init -b main
git add .
git commit -m "Initial public AIVOYA Acquisition Fabric V1.1"
git remote add origin https://github.com/idpxyz/aivoya-acquisition.git
git push -u origin main
```

**Review the staged files (`git status`, `git diff --cached --name-only`) before publishing.**
The actual 77-source registry is excluded from this public distribution; import it privately.

## Verification

After the push, open `https://github.com/idpxyz/aivoya-acquisition/actions`.
The PostgreSQL 16 integration and Python tests run on push. Note that `go.sum` is currently not committed; the workflow captures dependency resolution output, which should be reviewed and committed before production acceptance. Live GitHub API and Scrapling tests
are separately gated behind `workflow_dispatch`, and should be run only after approval.
No private credentials may be committed or attached to CI artifacts.
