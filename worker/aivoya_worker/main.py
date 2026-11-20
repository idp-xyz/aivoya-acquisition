"""Poll leased jobs and acknowledge results over the versioned JSON contract."""
import json
import importlib.util
import logging
import os
import time
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
from .fetch import execute

logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'),format='%(asctime)s %(levelname)s %(message)s')
log=logging.getLogger('aivoya.worker')

def api_request(base,token,path,payload=None):
    data=json.dumps(payload).encode() if payload is not None else b'{}'
    req=Request(base+path,data=data,method='POST',headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        with urlopen(req,timeout=35) as r:
            if r.status==204:return None
            return json.loads(r.read())
    except HTTPError as e:
        if e.code==409:
            log.warning('lease rejected as stale; result safely discarded')
            return None
        raise

def main():
    base=os.getenv('AIVOYA_API_URL','http://api:8080').rstrip('/')
    token=os.getenv('AIVOYA_WORKER_TOKEN','')
    engine=os.getenv('AIVOYA_FETCH_ENGINE','stdlib')
    if len(token)<24:raise SystemExit('AIVOYA_WORKER_TOKEN must have >=24 chars')
    if engine not in ('stdlib','scrapling'):raise SystemExit('Invalid AIVOYA_FETCH_ENGINE')
    if engine=='scrapling' and importlib.util.find_spec('scrapling') is None:
        raise SystemExit('Scrapling engine requested but not installed; rebuild worker with INSTALL_SCRAPLING=true')
    if engine=='scrapling' and os.getenv('AIVOYA_ENV')=='production' and os.getenv('AIVOYA_EGRESS_GUARD_VERIFIED')!='true':
        raise SystemExit('Refusing production browser/HTTP Scrapling without verified external egress isolation')
    log.info('worker started engine=%s (Scrapling only enabled explicitly)',engine)
    failures=0
    while True:
        try:
            job=api_request(base,token,'/v1/jobs/claim')
            failures=0
            if job:
                log.info('claim job_id=%s source=%s method=%s',job['job_id'],job['source_id'],job['method'])
                result=execute(job,engine=engine)
                response=api_request(base,token,'/v1/jobs/complete',result)
                log.info('result job_id=%s status=%s accepted=%s',job['job_id'],result['status'],bool(response))
            else:time.sleep(3)
        except (HTTPError,URLError,TimeoutError,OSError,ValueError) as e:
            failures+=1;log.error('worker transport error: %s',e)
            time.sleep(min(30,2**min(failures,5)))

if __name__=='__main__': main()
