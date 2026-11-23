"""Default-deny URL / robots policy. Stdlib sockets are pinned; network isolation is still required."""
from __future__ import annotations
import contextvars
import http.client
import ipaddress
import os
import socket
import time
from functools import partial
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.request import Request, build_opener, HTTPHandler, HTTPRedirectHandler, HTTPSHandler, ProxyHandler
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

# Addresses accepted for the controlled_get currently on the stack. Connect
# consumes the entry so the next redirect hop resolves again, exactly once.
_PINS: contextvars.ContextVar[dict | None] = contextvars.ContextVar("aivoya_egress_pins", default=None)

def egress_proxy():
    """Return the explicit acquisition proxy, or None when it is unset.

    Only ``http://host:port`` is accepted. Credentials and any other scheme
    are rejected so a typo cannot silently disable address pinning.
    """
    raw = os.environ.get("AIVOYA_EGRESS_PROXY", "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise PolicyBlocked("AIVOYA_EGRESS_PROXY must be an http://host:port URL without credentials") from exc
    if parts.scheme != "http" or parts.username or parts.password or parts.fragment or parts.query:
        raise PolicyBlocked("AIVOYA_EGRESS_PROXY must be an http://host:port URL without credentials")
    if not parts.hostname or port is None or not 1 <= port <= 65535 or parts.path not in ("", "/"):
        raise PolicyBlocked("AIVOYA_EGRESS_PROXY must be an http://host:port URL without credentials")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"http://{host}:{port}"

def _pin_key(host, port):
    host = (host or "").strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.rstrip("."), int(port)

def _literal_ip(host):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None

def _resolve_ips(host, port):
    """Resolve once. Numeric addresses are not passed to the resolver."""
    key_host, key_port = _pin_key(host, port)
    literal = _literal_ip(key_host)
    if literal is not None:
        return [literal]
    found = []
    seen = set()
    for item in socket.getaddrinfo(key_host, key_port, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(item[4][0])
        if ip in seen:
            continue
        seen.add(ip)
        found.append(ip)
    return found

def _reject_non_global(ips, allow_private):
    if not allow_private and (not ips or any(not ip.is_global for ip in ips)):
        raise PolicyBlocked("Host resolves to a private or non-global address")

def _store_pin(host, port, ips):
    store = _PINS.get()
    if store is None or not ips:
        return
    store[_pin_key(host, port)] = list(ips)

def _take_pin(host, port):
    store = _PINS.get()
    if store is None:
        return None
    return store.pop(_pin_key(host, port), None)

def _targets_for_connect(host, port, *, allow_private):
    """Addresses this socket may use. Checked here so no opener path can skip it."""
    ips = _take_pin(host, port)
    if ips is None:
        try:
            ips = _resolve_ips(host, port)
        except OSError as exc:
            raise TransientNetworkError(f"DNS resolution failed: {exc}") from exc
        except ValueError as exc:
            raise PolicyBlocked(f"Invalid URL: {exc}") from exc
    _reject_non_global(ips, allow_private)
    if not ips:
        raise TransientNetworkError("DNS resolution failed: no addresses")
    return ips

def _tcp_connect(targets, port, timeout, source_address):
    """Try validated addresses in order. The hostname is never resolved here."""
    last = None
    for ip in targets:
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            if ip.version == 6:
                sock.connect((str(ip), port, 0, 0))
            else:
                sock.connect((str(ip), port))
            return sock
        except OSError as exc:
            sock.close()
            last = exc
    if last is None:
        raise OSError("no validated address")
    raise last

def _install_pinned_connect(conn):
    targets = _targets_for_connect(conn.host, conn.port, allow_private=conn.allow_private)

    def create(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *, all_errors=False):
        # address[0] is the original hostname. Connect only to the pinned IPs.
        return _tcp_connect(targets, address[1], timeout, source_address)

    conn._create_connection = create

class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connect to the validated address. Leave Host as the URL hostname."""

    def __init__(self, *args, allow_private=False, **kwargs):
        self.allow_private = allow_private
        super().__init__(*args, **kwargs)

    def connect(self):
        _install_pinned_connect(self)
        super().connect()

class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Same pin as HTTP. stdlib then wraps TLS with server_hostname=self.host."""

    def __init__(self, *args, allow_private=False, **kwargs):
        self.allow_private = allow_private
        super().__init__(*args, **kwargs)

    def connect(self):
        _install_pinned_connect(self)
        super().connect()

class _PinHTTPHandler(HTTPHandler):
    def __init__(self, allow_private=False):
        super().__init__()
        self.allow_private = allow_private

    def http_open(self, req):
        return self.do_open(partial(_PinnedHTTPConnection, allow_private=self.allow_private), req)

class _PinHTTPSHandler(HTTPSHandler):
    def __init__(self, allow_private=False):
        super().__init__()
        self.allow_private = allow_private

    def https_open(self, req):
        return self.do_open(
            partial(_PinnedHTTPSConnection, allow_private=self.allow_private),
            req,
            context=self._context,
        )

class _ForcedProxyHandler(ProxyHandler):
    """Use only the operator proxy.

    ProxyHandler.proxy_open honors no_proxy and would otherwise connect
    directly, which this mode does not pin.
    """

    def proxy_open(self, req, proxy, type):
        orig_type = req.type
        parts = urlsplit(proxy)
        proxy_type = parts.scheme or orig_type
        host = parts.hostname
        if ":" in host:
            host = f"[{host}]"
        req.set_proxy(f"{host}:{parts.port}", proxy_type)
        if orig_type == proxy_type or orig_type == "https":
            return None
        return self.parent.open(req, timeout=req.timeout)

def _acquisition_opener(url, *, allow_private, proxy):
    redirect = SafeRedirect(url, allow_private=allow_private, skip_dns=proxy is not None)
    if proxy:
        return build_opener(redirect, _ForcedProxyHandler({"http": proxy, "https": proxy}))
    return build_opener(
        redirect,
        ProxyHandler({}),
        _PinHTTPHandler(allow_private),
        _PinHTTPSHandler(allow_private),
    )

def validate_url(raw: str, *, allow_private=False, resolve=True, skip_dns=False) -> str:
    """Reject URLs that are not allowed out.

    ``resolve=False`` keeps the historical check and does not query DNS.
    ``skip_dns=True`` is egress-proxy mode: names are left to the proxy, while
    literal private or non-global addresses are still rejected.
    """
    try:
        u = urlsplit(raw)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.fragment:
            raise PolicyBlocked("URL must be an absolute http(s) address without credentials/fragment")
        if u.port not in (None, 80, 443):
            raise PolicyBlocked("Nonstandard destination port blocked")
        host = u.hostname.rstrip('.').lower()
        if host in ("localhost",) or host.endswith((".local", ".localhost", ".internal")):
            if not allow_private: raise PolicyBlocked("Local host blocked")
        port = u.port or (443 if u.scheme == "https" else 80)
        literal = _literal_ip(host)
        if skip_dns and literal is None:
            return raw
        if literal is not None:
            ips = [literal]
        elif resolve:
            try:
                ips = _resolve_ips(host, port)
            except OSError as e:
                raise TransientNetworkError(f"DNS resolution failed: {e}") from e
        else:
            ips = []
        _reject_non_global(ips, allow_private)
        if resolve and not skip_dns:
            _store_pin(host, port, ips)
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
    def __init__(self, initial_url, allow_private=False, skip_dns=False):
        super().__init__()
        self.initial_url = initial_url
        self.allow_private = allow_private
        self.skip_dns = skip_dns
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, allow_private=self.allow_private, skip_dns=self.skip_dns)
        if not same_origin_host(newurl, self.initial_url):
            raise PolicyBlocked("Cross-origin redirect blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def controlled_get(url: str,*,timeout=25,max_bytes=2*1024*1024,allow_private=False,headers=None) -> tuple[bytes,int,str,str]:
    proxy = egress_proxy()
    token = None if proxy else _PINS.set({})
    try:
        validate_url(url, allow_private=allow_private, skip_dns=proxy is not None)
        op = _acquisition_opener(url, allow_private=allow_private, proxy=proxy)
        request_headers={"User-Agent":AGENT,"Accept":"text/html,application/xml,application/rss+xml,application/atom+xml;q=0.9,*/*;q=0.4"}
        if headers:
            # Third-party headers may only be supplied by trusted local adapters,
            # never copied from an untrusted source registry record.
            request_headers.update(headers)
        req=Request(url,headers=request_headers)
        try:
            with op.open(req,timeout=timeout) as r:
                final=r.url
                # Do not resolve again. Direct mode already pinned this hop; proxy
                # mode leaves destination DNS to the proxy. Literals are still checked.
                validate_url(final, allow_private=allow_private, skip_dns=True)
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
    finally:
        if token is not None:
            _PINS.reset(token)

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
    url=job.get('url','');validate_url(url,allow_private=allow_private,skip_dns=egress_proxy() is not None)
    if job.get('method')=='github_releases':
        from .extract import github_repo_from_api
        try: github_repo_from_api(url)
        except ValueError as exc: raise PolicyBlocked(f'Unapproved GitHub API endpoint: {exc}') from exc
    if not robots_permits(url,allow_private=allow_private):raise PolicyBlocked('robots.txt disallows access or is unavailable')
    return url
