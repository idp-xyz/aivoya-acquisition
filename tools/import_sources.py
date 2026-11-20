#!/usr/bin/env python3
"""Idempotent source registry importer. All new entries remain unapproved."""
import argparse,json,os,sys
from _settings import get
from urllib.request import Request,urlopen

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--file',default='data/source_registry.sample.json');ap.add_argument('--url',default=os.getenv('AIVOYA_API_URL','http://127.0.0.1:8080'))
    args=ap.parse_args()
    token=get('AIVOYA_ADMIN_TOKEN')
    if len(token)<24:raise SystemExit('AIVOYA_ADMIN_TOKEN is required')
    with open(args.file,encoding='utf8') as f:data=json.load(f)
    if not isinstance(data.get('sources'),list):raise SystemExit('Expecting {"sources": [...]}')
    imported=0
    for offset in range(0,len(data['sources']),500):
        batch={'sources':data['sources'][offset:offset+500]}
        req=Request(args.url.rstrip('/')+'/v1/sources/import',data=json.dumps(batch,ensure_ascii=False).encode(),method='POST',headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
        with urlopen(req,timeout=30) as r:out=json.load(r);imported+=out['imported']
    print(json.dumps({'accepted':imported,'note':'Sources pending/disabled until reviewed'},ensure_ascii=False))
if __name__=='__main__':main()
