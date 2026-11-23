"""Deterministic HTML article extraction without LLM hallucination."""
from __future__ import annotations
from html.parser import HTMLParser
from html import unescape
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from xml.parsers import expat
from urllib.parse import urljoin,urlsplit
from .policy import normalize_url, same_origin_host
import re
import json

BLOCK_TAGS = {'script','style','noscript','svg','form','nav','footer','header','aside','button'}
ARTICLE_TAGS = {'article','main'}

class Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack=[]; self.ignore=0;self.title=[];self.h1=[];self.by_scope={'article':[],'main':[],'body':[]}
        self.scope=[];self.metadata={};self.lang='und';self.in_title=False;self.in_h1=False
        self.jsonld=[];self._jsonld_current=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs);self.stack.append(tag)
        if tag in BLOCK_TAGS:self.ignore+=1
        if tag=='script' and (attrs.get('type') or '').strip().lower()=='application/ld+json':self._jsonld_current=[]
        if tag=='html' and attrs.get('lang'):self.lang=attrs['lang'][:12]
        if tag=='meta':
            name=(attrs.get('property') or attrs.get('name') or '').lower()
            if name and attrs.get('content'):self.metadata[name]=attrs['content']
        if tag=='title':self.in_title=True
        if tag=='h1':self.in_h1=True
        if tag in ARTICLE_TAGS:self.scope.append(tag)
    def handle_endtag(self,tag):
        if tag=='script' and self._jsonld_current is not None:
            self.jsonld.append(''.join(self._jsonld_current));self._jsonld_current=None
        if tag in BLOCK_TAGS:self.ignore=max(0,self.ignore-1)
        if tag=='title':self.in_title=False
        if tag=='h1':self.in_h1=False
        if tag in ARTICLE_TAGS and self.scope:
            for i in range(len(self.scope)-1,-1,-1):
                if self.scope[i]==tag:self.scope.pop(i);break
        if tag in {'p','div','li','h2','h3','section'} and not self.ignore:
            for k in self.by_scope:self.by_scope[k].append('\n')
        if tag in self.stack:
            # Handle malformed HTML without wiping all outer structure.
            i=len(self.stack)-1-self.stack[::-1].index(tag);self.stack=self.stack[:i]
    def handle_data(self,data):
        if self._jsonld_current is not None:
            if sum(len(v) for v in self._jsonld_current)<65536:self._jsonld_current.append(data)
            return
        if self.in_title:self.title.append(data)
        if self.in_h1:self.h1.append(data)
        if self.ignore:return
        if self.scope:
            for k in self.scope:self.by_scope[k].append(data+' ')
        self.by_scope['body'].append(data+' ')

def clean_text(s:str)->str:
    s=unescape(s).replace('\u00a0',' ')
    s=re.sub(r'[ \t]+',' ',s)
    s=re.sub(r'\n\s*\n+','\n',s)
    return s.strip()

def structured_article(jsonld: list[str])->dict:
    """Read publisher-provided Schema.org Article metadata (not AI-generated)."""
    for source in jsonld[:8]:
        try: doc=json.loads(source[:65536])
        except (TypeError,ValueError):continue
        queue=doc if isinstance(doc,list) else [doc]
        while queue:
            item=queue.pop(0)
            if not isinstance(item,dict):continue
            nodes=item.get('@graph')
            if isinstance(nodes,list):queue.extend(nodes[:40])
            kind=item.get('@type',[])
            kinds=[kind] if isinstance(kind,str) else kind if isinstance(kind,list) else []
            if any(str(t).lower().split('/')[-1] in ('article','newsarticle','blogposting','techarticle','scholarlyarticle') for t in kinds):
                return {'title':clean_text(str(item.get('headline') or '')),
                        'date':item.get('datePublished'),
                        'language':item.get('inLanguage'),
                        'body':clean_text(str(item.get('articleBody') or ''))}
    return {}

def article_from_html(payload:bytes, *,encoding='utf-8')->dict:
    raw=payload.decode(encoding,'replace')
    d=Document();d.feed(raw)
    ld=structured_article(d.jsonld)
    title=clean_text(' '.join(d.h1)) or clean_text(d.metadata.get('og:title','')) or ld.get('title','') or clean_text(' '.join(d.title))
    # Prefer the focused article body; fall back to main, then a bounded body.
    article_body=clean_text(''.join(d.by_scope['article']))
    main_body=clean_text(''.join(d.by_scope['main']))
    if len(article_body)>=60:body=article_body
    elif len(main_body)>=60:body=main_body
    else:body=clean_text(''.join(d.by_scope['body']))[:12000]
    if len(body)<40 and ld.get('body'):body=ld['body']
    date=d.metadata.get('article:published_time') or d.metadata.get('datepublished') or d.metadata.get('pubdate') or ld.get('date')
    language=d.lang if d.lang!='und' else ld.get('language','und')
    if not isinstance(language,str):language='und'
    return {'title':title[:500], 'body_text':body[:200000], 'language':language[:12], 'published_at':parse_date(date)}

def parse_date(value:str|None)->str|None:
    if not value:return None
    try:
        obj=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:
        try:obj=parsedate_to_datetime(value)
        except (ValueError,TypeError,OverflowError):return None
    if obj.tzinfo is None:obj=obj.replace(tzinfo=timezone.utc)
    return obj.astimezone(timezone.utc).isoformat().replace('+00:00','Z')

def parse_feed_xml(payload:bytes)->ET.Element:
    """Reject a real XML doctype or entity declaration.

    A byte search flags HTML inside CDATA, which legitimate feeds embed.
    Expat reports the actual declarations and leaves text content alone.
    """
    checker=expat.ParserCreate()
    def _forbid(*_args,**_kwargs):
        raise ValueError('XML DTD is forbidden')
    checker.StartDoctypeDeclHandler=_forbid
    checker.EntityDeclHandler=_forbid
    checker.UnparsedEntityDeclHandler=_forbid
    checker.NotationDeclHandler=_forbid
    checker.ExternalEntityRefHandler=_forbid
    try:
        checker.Parse(payload,True)
    except expat.ExpatError as exc:
        raise ValueError(f'Malformed XML: {exc}') from exc
    return ET.fromstring(payload)

def feed_entries(payload:bytes,base_url:str,*,max_entries=40,origin_url='')->list[dict]:
    root=parse_feed_xml(payload)
    result=[];seen=set()
    for item in list(root.iter()):
        tag=item.tag.rsplit('}',1)[-1].lower()
        if tag not in ('item','entry'):continue
        title='';link='';date=None
        for v in list(item):
            k=v.tag.rsplit('}',1)[-1].lower()
            if k=='title':title=clean_text(''.join(v.itertext()))
            elif k=='link':
                href=v.get('href') or (v.text or '').strip()
                if (v.get('rel') in (None,'alternate')) and href:link=href
            elif k in ('pubdate','published','updated','date'):date=parse_date((v.text or '').strip())
        if not title or not link:continue
        link=urljoin(base_url,link)
        if not (same_origin_host(link,base_url) or (origin_url and same_origin_host(link,origin_url))):continue
        try:link=normalize_url(link)
        except ValueError:continue
        if link in seen:continue
        seen.add(link);result.append({'url':link,'title':title[:500],'published_at':date})
        if len(result)>=max_entries:break
    return result

class LinkCollector(HTMLParser):
    """Candidate discovery only, not a claim that a URL is an article."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchor=None;self.links=[];self.in_block=0
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in ('nav','footer','header'):self.in_block+=1
        if tag=='a' and not self.in_block and attrs.get('href'):
            self.anchor=[attrs.get('href'),[]]
    def handle_endtag(self,tag):
        if tag=='a' and self.anchor:
            url,parts=self.anchor;self.links.append((url,clean_text(' '.join(parts))));self.anchor=None
        if tag in ('nav','footer','header'):self.in_block=max(0,self.in_block-1)
    def handle_data(self,data):
        if self.anchor:self.anchor[1].append(data)

def index_entries(payload:bytes,base_url:str,*,max_entries=40,origin_url='')->list[dict]:
    """Conservative link discovery; production should customize selectors per source."""
    d=LinkCollector();d.feed(payload.decode('utf-8','replace'))
    candidate=[];seen=set()
    homepage=urlsplit(base_url).path.rstrip('/')
    for href,title in d.links:
        if len(title)<8 or len(title)>250:continue
        absolute=urljoin(base_url,href)
        if not (same_origin_host(absolute,base_url) or (origin_url and same_origin_host(absolute,origin_url))):continue
        u=urlsplit(absolute)
        if u.scheme not in ('http','https') or u.path.rstrip('/')==homepage or u.fragment:continue
        path=u.path.lower()
        # Avoid paging, categories, galleries, accounts, tags, and index loops.
        if any(x in path for x in ('/tag/','/category/','/author/','/login','/search','/about','/privacy','/topics/','/page/')):continue
        segment=[p for p in u.path.split('/') if p]
        if len(segment)<2 and not (len(segment)==1 and ('-' in segment[-1] or len(segment[-1])>18)):continue
        if segment and segment[-1].lower() in {'news','blog','research','articles','posts'}:continue
        absolute=normalize_url(absolute)
        if absolute in seen:continue
        seen.add(absolute)
        candidate.append({'url':absolute,'title':title,'published_at':None})
        if len(candidate)>=max_entries:break
    return candidate

# Structured official provider adapter. Metadata only; do not lift release-body
# content into licensed full-text fields or turn API URLs into generic crawls.
def github_repo_from_api(url: str) -> tuple[str, str]:
    """Return the exact owner/repo for an API releases collection URL."""
    from urllib.parse import parse_qs
    u=urlsplit(url)
    if u.scheme!='https' or u.netloc.lower()!='api.github.com' or u.fragment:
        raise ValueError('GitHub releases must use api.github.com over HTTPS')
    parts=[p for p in u.path.strip('/').split('/') if p]
    if len(parts)!=4 or parts[0]!='repos' or parts[3]!='releases':
        raise ValueError('Expected /repos/{owner}/{repo}/releases')
    if not all(re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',v) for v in parts[1:3]):
        raise ValueError('Invalid repository coordinates')
    if any(x not in ('per_page','page') for x in parse_qs(u.query)):
        raise ValueError('Only per_page and page query keys are permitted')
    args=parse_qs(u.query)
    for key,limit in [('per_page',100),('page',10000)]:
        if key in args and (len(args[key])!=1 or not args[key][0].isdigit() or not 1<=int(args[key][0])<=limit):
            raise ValueError('Invalid GitHub API pagination parameter')
    return parts[1],parts[2]

def github_release_entries(payload:bytes, base_url:str, *,max_entries=40) -> list[dict]:
    """Parse public GitHub Release metadata from the official REST API."""
    import json
    owner,repo=github_repo_from_api(base_url)
    decoded=json.loads(payload)
    if not isinstance(decoded,list):
        raise ValueError('GitHub releases response must be an array')
    result=[]; seen=set()
    for release in decoded[:200]:
        if not isinstance(release,dict) or release.get('draft'):
            continue
        web=release.get('html_url','')
        try:
            u=urlsplit(web)
            parts=[v for v in u.path.strip('/').split('/') if v]
            if (u.scheme!='https' or u.netloc.lower()!='github.com' or u.query or u.fragment
                or len(parts)!=5 or parts[0].lower()!=owner.lower() or parts[1].lower()!=repo.lower()
                or parts[2:4]!=['releases','tag'] or not parts[4]):
                continue
        except (TypeError,ValueError): continue
        if web in seen:continue
        seen.add(web)
        title=clean_text(str(release.get('name') or release.get('tag_name') or ''))
        if not title:continue
        dt=parse_date(release.get('published_at'))
        result.append({'url':web,'title':title[:500], 'published_at':dt})
        if len(result)>=max_entries:break
    return result
