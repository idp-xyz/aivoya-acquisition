"""Acquisition execution; Scrapling is an opt-in adapter, never a policy authority."""
import base64
import time
from datetime import datetime,timezone
from .policy import enforce,controlled_get,PolicyBlocked,TransientNetworkError,validate_url,same_origin_host
from .extract import article_from_html,feed_entries,index_entries,github_release_entries

MAX_RAW=2*1024*1024

def _scrapling_get(url: str,method: str):
    try:
        from scrapling.fetchers import Fetcher,StealthyFetcher
        import scrapling
    except ImportError as e:
        raise RuntimeError('Scrapling unavailable; install optional worker dependencies') from e
    if method in ('html_static','html_index','monitor'):
        page=Fetcher.get(url)
    else:
        # Do not use solve_cloudflare or proxy rotation to circumvent access controls.
        page=StealthyFetcher.fetch(url,headless=True,solve_cloudflare=False)
    raw=page.body
    if isinstance(raw,str):raw=raw.encode('utf-8')
    if len(raw)>MAX_RAW:raise PolicyBlocked('Response exceeds 2 MiB')
    final=str(getattr(page,'url',url) or url)
    if not same_origin_host(final,url):raise PolicyBlocked('Redirected to an unregistered host')
    validate_url(final)
    return raw,int(page.status),final,str(getattr(page,'headers',{}).get('content-type','text/html')),str(getattr(scrapling,'__version__','0.5.1'))

def execute(job:dict,*,engine='stdlib',allow_private=False)->dict:
    start=time.monotonic()
    result={'schema_version':'1.0','job_id':job['job_id'],'lease_token':job['lease_token'],'status':'retryable_error','method_used':job.get('method',''), 'http_status':0, 'acquired_at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z')}
    try:
        url=enforce(job,allow_private=allow_private)
        method=job['method'];version='stdlib-1.0'
        if method in ('rss','github_releases') or engine=='stdlib':
            if method=='browser_render':raise PolicyBlocked('Browser renderer requires the Scrapling adapter')
            headers=None
            if method=='github_releases':
                import os
                headers={'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}
                token=os.getenv('AIVOYA_GITHUB_TOKEN','')
                if token:headers['Authorization']='Bearer '+token
            raw,http_status,final,media=controlled_get(url,timeout=25,max_bytes=MAX_RAW,allow_private=allow_private,**({'headers':headers} if headers else {}))
        elif engine=='scrapling':
            # Requires an outbound egress policy external to this process; no DNS pinning
            # in Scrapling's redirects. Never enable with unreviewed arbitrary URLs.
            raw,http_status,final,media,version=_scrapling_get(url,method)
        else:raise PolicyBlocked('Unknown acquisition engine')
        if http_status<200 or http_status>=300:raise TransientNetworkError(f'HTTP {http_status}')
        result.update({'status':'success','http_status':http_status,'final_url':final,'media_type':media,
                       'raw_base64':base64.b64encode(raw).decode('ascii'), 'extractor_name':('scrapling' if engine=='scrapling' and method!='rss' else 'stdlib-html'), 'extractor_version':version})
        if method=='rss':
            result['entries']=feed_entries(raw,final,origin_url=job.get('origin_url',''))
            result['extractor_name']='rss-atom-parser'
        elif method=='html_index':
            result['entries']=index_entries(raw,final,origin_url=job.get('origin_url',''))
            result['extractor_name']='html-index-parser'
        elif method=='github_releases':
            result['entries']=github_release_entries(raw,final)
            result['extractor_name']='github-official-api'
            result['extractor_version']='2022-11-28'
        else:
            if 'pdf' in media.lower():raise PolicyBlocked('PDF belongs in the document pipeline')
            result.update(article_from_html(raw))
    except PolicyBlocked as e:result.update({'status':'blocked','error_code':'policy_blocked','error':str(e)})
    except TransientNetworkError as e:result.update({'status':'retryable_error','error_code':'rate_limited' if e.http_status in (429,503) else 'network_retryable','error':str(e),'http_status':e.http_status,'retry_after_seconds':e.retry_after_seconds})
    except (ValueError,UnicodeError) as e:result.update({'status':'permanent_error','error_code':'invalid_content','error':str(e)})
    except Exception as e:result.update({'status':'retryable_error','error_code':'adapter_error','error':str(e)[:500]})
    result['duration_ms']=round((time.monotonic()-start)*1000)
    return result
