#!/usr/bin/env python3
"""Create strong, distinct local deployment credentials; never overwrite secrets."""
import os,secrets,stat
from pathlib import Path

def main():
 p=Path('.env')
 if p.exists():raise SystemExit('.env already exists; no credentials have been changed')
 data='\n'.join([f'{key}={secrets.token_urlsafe(36)}' for key in ('POSTGRES_PASSWORD','AIVOYA_ADMIN_TOKEN','AIVOYA_WORKER_TOKEN')])+"\nINSTALL_BROWSER=false\nINSTALL_SCRAPLING=false\nAIVOYA_FETCH_ENGINE=stdlib\nAIVOYA_ENV=development\nAIVOYA_EGRESS_GUARD_VERIFIED=false\nAIVOYA_GITHUB_TOKEN=\nAIVOYA_USER_AGENT=AIVOYA-Acquisition/1.1\n"
 flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
 fd=os.open(str(p),flags,0o600)
 try:
  with os.fdopen(fd,'w',encoding='utf8') as f:f.write(data)
 except BaseException:
  p.unlink(missing_ok=True)
  raise
 print('Generated .env with three distinct secrets; permissions 0600 on POSIX.')
 print('The console admin token is on the AIVOYA_ADMIN_TOKEN line of the local .env file.')
if __name__=='__main__':main()
