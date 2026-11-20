#!/usr/bin/env python3
import json,os
from _settings import get
from urllib.request import Request,urlopen

def main():
 base=os.getenv('AIVOYA_API_URL','http://127.0.0.1:8080').rstrip('/')
 token=get('AIVOYA_ADMIN_TOKEN')
 if not token:raise SystemExit('Set AIVOYA_ADMIN_TOKEN')
 for path in ('/healthz','/readyz','/v1/stats','/v1/sources','/v1/jobs','/v1/records','/v1/operations/source-health'):
  req=Request(base+path,headers={'Authorization':'Bearer '+token})
  with urlopen(req,timeout=8) as r:
   data=json.load(r);print(path,r.status,'items='+str(len(data)) if isinstance(data,list) else json.dumps(data,ensure_ascii=False))
 print('PASS: authenticated API read paths')
if __name__=='__main__':main()
