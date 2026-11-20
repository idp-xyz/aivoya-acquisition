# Run inside the extracted project directory after reviewing files.
$ErrorActionPreference = 'Stop'
$repo = 'idpxyz/aivoya-acquisition'
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'GitHub CLI (gh) is required: https://cli.github.com/' }
& gh auth status
if ($LASTEXITCODE -ne 0) { throw 'Authenticate using: gh auth login' }
if (-not (Test-Path '.git')) {
    & git init -b main
    if ($LASTEXITCODE -ne 0) { throw 'git init failed' }
}
& git add .
if ($LASTEXITCODE -ne 0) { throw 'git add failed' }
Write-Host 'Files to publish (check carefully):'
& git diff --cached --name-only
Read-Host 'Review the list, then press Enter to commit/publish (Ctrl+C to cancel)'
& git commit -m 'Initial public AIVOYA Acquisition Fabric V1.1'
if ($LASTEXITCODE -ne 0) { throw 'git commit failed' }
& git remote add origin "https://github.com/$repo.git"
if ($LASTEXITCODE -ne 0) { throw 'git remote add failed (if remote exists, skip this step manually)' }
& git push -u origin main
if ($LASTEXITCODE -ne 0) { throw 'GitHub push failed' }
Write-Host "Repo: https://github.com/$repo"
