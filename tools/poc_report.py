#!/usr/bin/env python3
"""Aggregate paired PoC output; no marketing benchmark substitutions."""
import json,sys,statistics,collections
from pathlib import Path

def report(lines):
 data=[json.loads(l) for l in lines if l.strip()]
 by=collections.defaultdict(list)
 for row in data:by[(row['cohort'],row['engine'])].append(row['metrics'])
 result=[]
 for (cohort,engine),a in sorted(by.items()):
  n=len(a);valid=sum(int(x['valid_fetch'] and x['text_chars']>=120 and x['title_present']) for x in a)
  result.append({'cohort':cohort,'engine':engine,'attempts':n,'heuristic_valid':valid,'valid_rate':round(valid/n,4),'mean_ms':round(statistics.mean(x['duration_ms'] for x in a)),'p95_ms':sorted(x['duration_ms'] for x in a)[min(n-1,int(.95*n))]})
 return {'experimental_only':True,'note':'Not a substitute for the 140 manually labeled ground truth samples.','groups':result}
if __name__=='__main__':
 if len(sys.argv)!=2:raise SystemExit('usage: python tools/poc_report.py results/poc.jsonl')
 print(json.dumps(report(Path(sys.argv[1]).read_text(encoding='utf8').splitlines()),ensure_ascii=False,indent=2))
