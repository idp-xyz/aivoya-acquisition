#!/usr/bin/env python3
"""Paired offline-or-online experiment runner; no invented benchmark results."""
import argparse,json,sys,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'worker'))
from aivoya_worker.fetch import execute
from aivoya_worker.policy import validate_url

def metric(entry):
 ok=entry.get('status')=='success'
 return {'valid_fetch':ok,'duration_ms':entry.get('duration_ms',0),'raw_bytes':(len(entry.get('raw_base64',''))*3//4),'text_chars':len(entry.get('body_text','')),'title_present':bool(entry.get('title')),'error_code':entry.get('error_code')}

def run(config,out):
 cases=config.get('targets',[])
 if not cases:raise ValueError('PoC has no targets. Fill only legally approved test pages.')
 out.parent.mkdir(parents=True,exist_ok=True)
 with out.open('a',encoding='utf8') as f:
  for index,item in enumerate(cases):
   if item.get('rights') not in ('metadata_only','extract_only','full_text_licensed') or len(item.get('approval_basis',''))<12:raise ValueError('Every PoC source requires explicit rights and approval_basis')
   validate_url(item['url'])
   if item.get('method') not in ('html_static','html_index','monitor'):
    raise ValueError('PoC only compares HTML acquisition methods')
   methods=['stdlib','scrapling'] if index%2==0 else ['scrapling','stdlib']
   for engine in methods:
    job={'schema_version':'1.0','job_id':f'poc-{index}-{engine}','lease_token':'poc-only','url':item['url'],'method':item['method'],'rights':item['rights']}
    v=execute(job,engine=engine)
    row={'url':item['url'],'cohort':item.get('cohort','unknown'),'engine':engine,'rights':item['rights'],'metrics':metric(v)}
    f.write(json.dumps(row,ensure_ascii=False)+'\n');f.flush()
    print(row['url'],engine,row['metrics'])

def main():
 p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--out',required=True);a=p.parse_args()
 with open(a.config,encoding='utf8') as f:config=json.load(f)
 run(config,Path(a.out))
if __name__=='__main__':main()
