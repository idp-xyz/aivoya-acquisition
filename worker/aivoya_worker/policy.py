"""Default-deny URL / robots policy. Network isolation is still required in production."""
from __future__ import annotations
import ipaddress
import os
import socket
import time
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.robotparser import RobotFileParser
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

ROBOTS_CACHE: dict[str, tuple[float, object]] = {}
AGENT = os.getenv("AIVOYA_USER_AGENT", "AIVOYA-Acquisition/1.0 (research ingestion; configure operator contact)")
TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid"}

class PolicyBlocked(Exception): pass
class TransientNetworkError(Exception):
    def __init__(self, message, *, http_status=0, retry_after_seconds=0):
        super().__init__(message)
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds

def retry_delay(headers, *, now=None):
    """Server-directed Retry-After seconds, HTTP date, or GitHub rate reset.
    Bound the value so that a bad server cannot stall our entire scheduler.
    """
    raw = headers.get('Retry-After', '')
    if raw:
        try: return max(1, min(3600, int(raw)))
        except (ValueError, TypeError):
            try:
                dt=parsedate_to_datetime(raw)
                if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
                if now is None:now=datetime.now(timezone.utc)
                return max(1, min(3600, int((dt-now).total_seconds())+1))
            except (ValueError, OverflowError, TypeError):pass
    reset=headers.get('X-RateLimit-Reset')
    if reset and headers.get('X-RateLimit-Remaining')=='0':
        try:
            if now is None:now=datetime.now(timezone.utc)
            return max(1,min(3600,int(reset)-int(now.timestamp())+1))
        except (TypeError, ValueError):pass
    return 0

def validate_url(raw: str, *, allow_private=False, resolve=True) -> str:
    try:
        u = urlsplit(raw)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.fragment:
            raise PolicyBlocked("URL must be an absolute http(s) address without credentials/fragment")
        if u.port not in (None, 80, 443):
            raise PolicyBlocked("Nonstandard destination port blocked")
        host = u.hostname.rstrip('.').lower()
        if host in ("localhost",) or host.endswith((".local", ".localhost", ".internal")):
            if not allow_private: raise PolicyBlocked("Local host blocked")
        try:
            ips=[ipaddress.ip_address(host)]
        except ValueError:
            if resolve:
                try: ips=[ipaddress.ip_address(a[4][0]) for a in socket.getaddrinfo(host, u.port or (443 if u.scheme == 'https' else 80), type=socket.SOCK_STREAM)]
                except OSError as e: raise TransientNetworkError(f"DNS resolution failed: {e}") from e
            else: ips=[]
        if not allow_private and (not ips or any(not ip.is_global for ip in ips)):
            raise PolicyBlocked("Host resolves to a private or non-global address")
        return raw
    except ValueError as e:
        raise PolicyBlocked(f"Invalid URL: {e}") from e

def normalize_url(raw: str) -> str:
    u=urlsplit(raw)
    q=[(k,v) for k,v in parse_qsl(u.query, keep_blank_values=True) if k.lower() not in TRACKING]
    return urlunsplit((u.scheme.lower(),u.netloc.lower(),u.path or '/',urlencode(q),'')).rstrip('#')

def same_origin_host(left: str, right: str) -> bool:
    a,b=urlsplit(left).hostname,urlsplit(right).hostname
    if not a or not b: return False
    a=a.rstrip('.').lower();b=b.rstrip('.').lower()
    return a == b

class SafeRedirect(HTTPRedirectHandler):
    """Validate every hop; never permit an unregistered redirect to another host."""
    def __init__(self, initial_url,allow_private=False):
        super().__init__(); self.initial_url=initial_url;self.allow_private=allow_private
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl,allow_private=self.allow_private)
        if not same_origin_host(newurl,self.initial_url):
            raise PolicyBlocked("Cross-origin redirect blocked")
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def controlled_get(url: str,*,timeout=25,max_bytes=2*1024*1024,allow_private=False,headers=None) -> tuple[bytes,int,str,str]:
    validate_url(url,allow_private=allow_private)
    op=build_opener(SafeRedirect(url,allow_private=allow_private))
    request_headers={"User-Agent":AGENT,"Accept":"text/html,application/xml,application/rss+xml,application/atom+xml;q=0.9,*/*;q=0.4"}
    if headers:
        # Third-party headers may only be supplied by trusted local adapters,
        # never copied from an untrusted source registry record.
        request_headers.update(headers)
    req=Request(url,headers=request_headers)
    try:
        with op.open(req,timeout=timeout) as r:
            final=r.url
            validate_url(final,allow_private=allow_private)
            if not same_origin_host(final,url):raise PolicyBlocked("Unexpected response host")
            size=r.headers.get("Content-Length")
            if size is not None and int(size)>max_bytes:raise PolicyBlocked("Content exceeds acquisition size budget")
            payload=r.read(max_bytes+1)
            if len(payload)>max_bytes:raise PolicyBlocked("Response exceeds acquisition size budget")
            return payload,r.status,final,r.headers.get("Content-Type", "application/octet-stream")
    except HTTPError as e:
        if e.code in (429,500,502,503,504) or (e.code==403 and e.headers.get('X-RateLimit-Remaining')=='0'):
            raise TransientNetworkError(f"HTTP transient {e.code}",http_status=e.code,retry_after_seconds=retry_delay(e.headers)) from e
        if e.code in (401,403,451):raise PolicyBlocked(f"HTTP policy rejection {e.code}") from e
        raise PolicyBlocked(f"HTTP status {e.code}") from e
    except (URLError,TimeoutError,OSError) as e:raise TransientNetworkError(f"Network error: {e}") from e

def robots_permits(url: str, *,timeout=8,allow_private=False) -> bool:
    """Fail closed; cache per origin for 15min (errors for 60s).

    404/410 means no robots policy. All other failures block collection.
    """
    u=urlsplit(url)
    origin=f"{u.scheme}://{u.netloc}"
    cached=ROBOTS_CACHE.get(origin)
    now=time.monotonic()
    if cached and cached[0]>now:
        rule=cached[1]
        return bool(rule) if isinstance(rule,bool) else rule.can_fetch(AGENT,url)
    try:
        payload,status,final,_=controlled_get(origin+'/robots.txt',timeout=timeout,max_bytes=256*1024,allow_private=allow_private)
        if status!=200:raise PolicyBlocked('robots.txt response not 200')
        parser=RobotFileParser();parser.parse(payload.decode('utf-8','replace').splitlines())
        ROBOTS_CACHE[origin]=(now+900,parser)
        return parser.can_fetch(AGENT,url)
    except PolicyBlocked as e:
        if "HTTP status 404" in str(e) or "HTTP status 410" in str(e):
            ROBOTS_CACHE[origin]=(now+900,True)
            return True
    except TransientNetworkError:pass
    ROBOTS_CACHE[origin]=(now+60,False)
    return False

def enforce(job: dict,*,allow_private=False) -> str:
    # Job rights always supplied by the Go control plane, never inferred by worker.
    if job.get('schema_version')!='1.0': raise PolicyBlocked('Unsupported task contract')
    if job.get('rights') not in {'metadata_only','extract_only','full_text_licensed'}:raise PolicyBlocked('Source rights are not approved')
    if job.get('method') not in {'rss','html_index','monitor','html_static','browser_render','github_releases'}:raise PolicyBlocked('Unsupported collection channel')
    url=job.get('url','');validate_url(url,allow_private=allow_private)
    if job.get('method')=='github_releases':
        from .extract import github_repo_from_api
        try: github_repo_from_api(url)
        except ValueError as exc: raise PolicyBlocked(f'Unapproved GitHub API endpoint: {exc}') from exc
    if not robots_permits(url,allow_private=allow_private):raise PolicyBlocked('robots.txt disallows access or is unavailable')
    return url
