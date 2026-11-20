"""No network tests for v1.1 acquisition routing, rights, and recovery semantics."""
import sys
import base64
import json
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aivoya_worker.policy import retry_delay,PolicyBlocked,TransientNetworkError,validate_url,enforce
from aivoya_worker.extract import github_release_entries,github_repo_from_api
from aivoya_worker.fetch import execute

API='https://api.github.com/repos/D4Vinci/Scrapling/releases?per_page=20'
RELEASES=json.dumps([
  {'name':'Scrapling v0.5.1', 'tag_name':'v0.5.1','html_url':'https://github.com/D4Vinci/Scrapling/releases/tag/v0.5.1','draft':False,'published_at':'2026-11-04T20:29:16Z'},
  {'name':'internal draft','tag_name':'v0.6.0-draft','html_url':'https://github.com/D4Vinci/Scrapling/releases/tag/v0.6.0-draft','draft':True},
  {'name':'wrong repository','tag_name':'v1','html_url':'https://github.com/D4Vinci/Another/releases/tag/v1','draft':False},
  {'name':'redirect outside scope','tag_name':'v2','html_url':'https://example.org/path','draft':False},
  {'name':'Scrapling v0.5.1', 'tag_name':'v0.5.1','html_url':'https://github.com/D4Vinci/Scrapling/releases/tag/v0.5.1','draft':False}
]).encode()

class TestGitHubOfficialConnector(unittest.TestCase):
    def test_endpoint_allowlist(self):
        self.assertEqual(github_repo_from_api(API),('D4Vinci','Scrapling'))
        for url in ('http://api.github.com/repos/a/b/releases', 'https://api.github.com/repos/a/b/issues',
                    'https://api.github.com.evil.test/repos/a/b/releases', 'https://api.github.com/repos/a/b/releases?token=secret',
                    'https://api.github.com/repos/a/b/releases?per_page=999'):
            with self.subTest(url=url), self.assertRaises(ValueError):github_repo_from_api(url)
    def test_parsed_only_authentic_releases(self):
        rows=github_release_entries(RELEASES,API)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['url'],'https://github.com/D4Vinci/Scrapling/releases/tag/v0.5.1')
        self.assertEqual(rows[0]['published_at'],'2026-11-04T20:29:16Z')
    def test_reject_unexpected_api_payload(self):
        with self.assertRaises(ValueError):github_release_entries(b'{"error":"rate limited"}',API)
    def test_execute_uses_official_api_with_no_fulltext(self):
        job={'schema_version':'1.0','job_id':'job_official','lease_token':'lease_official',
             'url':API,'method':'github_releases','rights':'metadata_only'}
        with patch('aivoya_worker.fetch.enforce',return_value=API),patch('aivoya_worker.fetch.controlled_get',return_value=(RELEASES,200,API,'application/json')) as getter:
            result=execute(job)
        self.assertEqual(result['status'],'success')
        self.assertEqual(result['extractor_name'],'github-official-api')
        self.assertNotIn('body_text',result)
        self.assertEqual(len(result['entries']),1)
        self.assertIn('Accept',getter.call_args.kwargs['headers'])
    def test_unapproved_github_path_blocked_before_fetch(self):
        job={'schema_version':'1.0','job_id':'job_wrong','lease_token':'lease_wrong','url':'https://api.github.com/users/definitely-not-releases','method':'github_releases','rights':'metadata_only'}
        with patch('aivoya_worker.policy.validate_url',return_value=job['url']),patch('aivoya_worker.policy.robots_permits',return_value=True),patch('aivoya_worker.fetch.controlled_get') as getter:
            result=execute(job)
        self.assertEqual(result['status'],'blocked')
        getter.assert_not_called()

class TestRateLimitPolicy(unittest.TestCase):
    def test_retry_after_seconds(self):
        self.assertEqual(retry_delay({'Retry-After':'120'}),120)
        self.assertEqual(retry_delay({'Retry-After':'999999'}),3600)
        self.assertEqual(retry_delay({'Retry-After':'nope'}),0)
    def test_retry_after_http_date(self):
        now=datetime(2026,11,21,0,0,tzinfo=timezone.utc)
        header=(now+timedelta(seconds=120)).strftime('%a, %d %b %Y %H:%M:%S GMT')
        self.assertGreaterEqual(retry_delay({'Retry-After':header},now=now),120)
    def test_github_limit_reset(self):
        now=datetime(2026,11,21,0,0,tzinfo=timezone.utc)
        headers={'X-RateLimit-Remaining':'0','X-RateLimit-Reset':str(int(now.timestamp())+240)}
        self.assertGreaterEqual(retry_delay(headers,now=now),240)
    def test_completion_propagates_http_retry(self):
        job={'schema_version':'1.0','job_id':'retry001','lease_token':'lease001',
             'url':'https://example.org/feed','method':'rss','rights':'metadata_only'}
        err=TransientNetworkError('HTTP transient 429',http_status=429,retry_after_seconds=300)
        with patch('aivoya_worker.fetch.enforce',return_value=job['url']),patch('aivoya_worker.fetch.controlled_get',side_effect=err):
            result=execute(job)
        self.assertEqual(result['status'],'retryable_error')
        self.assertEqual(result['http_status'],429)
        self.assertEqual(result['retry_after_seconds'],300)
        self.assertEqual(result['error_code'],'rate_limited')

if __name__=='__main__':unittest.main()

class TestChineseSourceAndStructuredExtraction(unittest.TestCase):
    def test_rss_origin_host_explicitly_allowed(self):
        from aivoya_worker.extract import feed_entries
        xml=b'<rss><channel><item><title>A research paper</title><link>https://arxiv.org/abs/2601.01234</link></item><item><title>Bad</title><link>https://evil.org/x</link></item></channel></rss>'
        self.assertEqual(feed_entries(xml,'https://rss.arxiv.org/rss/cs.AI'),[])
        rows=feed_entries(xml,'https://rss.arxiv.org/rss/cs.AI',origin_url='https://arxiv.org/list/cs.AI/recent')
        self.assertEqual([r['url'] for r in rows],['https://arxiv.org/abs/2601.01234'])
    def test_short_chinese_headlines_are_candidates(self):
        from aivoya_worker.extract import index_entries
        html='<main><a href="/news/2026/ai">国产大模型技术新进展</a></main>'.encode()
        self.assertEqual(len(index_entries(html,'https://example.com/news')),1)
    def test_schema_org_jsonld_fills_date_and_headline(self):
        from aivoya_worker.extract import article_from_html
        raw=b'''<html><head><script type="application/ld+json">{"@context":"https://schema.org","@type":"NewsArticle","headline":"Grounded report from source","datePublished":"2026-11-20T10:30:00Z"}</script></head><body><article><p>''' + b'Source-backed detailed article. '*20 + b'''</p></article></body></html>'''
        row=article_from_html(raw)
        self.assertEqual(row['title'],'Grounded report from source')
        self.assertEqual(row['published_at'],'2026-11-20T10:30:00Z')
        self.assertIn('Source-backed',row['body_text'])
