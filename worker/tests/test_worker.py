import sys
import unittest
import os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aivoya_worker.extract import article_from_html,feed_entries
from aivoya_worker.policy import validate_url,normalize_url,PolicyBlocked,same_origin_host
from aivoya_worker.fetch import execute

class TestSourcePolicy(unittest.TestCase):
    def test_rejects_internal(self):
        for url in ('http://127.0.0.1/path','http://169.254.169.254/','http://10.0.0.1/','file:///etc/passwd','http://localhost/a','https://abc.local/'):
            with self.subTest(url=url),self.assertRaises(PolicyBlocked):validate_url(url,resolve=False)
    def test_normalize(self):
        self.assertEqual(normalize_url('https://EXAMPLE.org/a?utm_source=x&x=1#frag'),'https://example.org/a?x=1')
    def test_boundary(self):
        self.assertTrue(same_origin_host('https://news.example.org/a','https://news.example.org/feed'))
        self.assertFalse(same_origin_host('https://news.example.org/a','https://example.org/feed'))
        self.assertFalse(same_origin_host('https://example.org.evil.test/a','https://example.org/rss'))

class TestExtraction(unittest.TestCase):
    def test_body_title(self):
        html=b'<html lang="zh-CN"><head><title>Browser</title><meta property="article:published_time" content="2026-11-21T10:00:00Z"></head><body><nav>advertisements</nav><article><h1>Research on autonomous agents</h1><p>Detailed reporting with many facts, citations, and research.</p></article></body></html>'
        p=article_from_html(html)
        self.assertEqual(p['title'],'Research on autonomous agents')
        self.assertNotIn('advertisements',p['body_text'])
        self.assertEqual(p['language'],'zh-CN')
        self.assertEqual(p['published_at'],'2026-11-21T10:00:00Z')
    def test_feed(self):
        xml=b'<rss><channel><item><title>A</title><link>https://example.com/p1</link><pubDate>Sat, 21 Nov 2026 01:00:00 GMT</pubDate></item><item><title>B</title><link>https://example.com.evil.com/p2</link></item></channel></rss>'
        items=feed_entries(xml,'https://example.com/feed')
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['url'],'https://example.com/p1')
    def test_xml_entity_denied(self):
        with self.assertRaises(ValueError):feed_entries(b'<!DOCTYPE f [<!ENTITY x "y">]><f/>','https://example.com/feed')
        with self.assertRaises(ValueError):feed_entries(b'<!DOCTYPE rss SYSTEM "http://evil.test/a.dtd"><rss/>','https://example.com/feed')
        with self.assertRaises(ValueError):feed_entries(b'<rss><unclosed></rss>','https://example.com/feed')
    def test_cdata_html_doctype_is_content(self):
        xml=b'''<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><title>Changelog entry</title><link>https://github.blog/changelog/one</link><pubDate>Sat, 21 Nov 2026 01:00:00 GMT</pubDate><content:encoded><![CDATA[<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.0 Transitional//EN"><p>Hi &amp; <!ENTITY x "y"></p>]]></content:encoded></item></channel></rss>'''
        items=feed_entries(xml,'https://github.blog/changelog/feed/')
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['url'],'https://github.blog/changelog/one')
        self.assertEqual(items[0]['published_at'],'2026-11-21T01:00:00Z')
    def test_denied_job_no_fetch(self):
        job={'schema_version':'1.0','job_id':'x','lease_token':'y','rights':'prohibited','method':'html_static','url':'https://example.com/'}
        res=execute(job)
        self.assertEqual(res['status'],'blocked')

if __name__=='__main__':unittest.main()

class TestDiscovery(unittest.TestCase):
    def test_news_index(self):
        from aivoya_worker.extract import index_entries
        html=b'<html><nav><a href="/blog/wrong-news">Hidden nav entry</a></nav><main><a href="/news/2026/new-frontier-model">New frontier model released with technical details</a><a href="/about">Our about page</a><a href="https://example.com.evil.org/blog/long-false-entry">External content not trusted</a></main></html>'
        entries=index_entries(html,'https://example.com/news')
        self.assertEqual(len(entries),1)
        self.assertEqual(entries[0]['url'],'https://example.com/news/2026/new-frontier-model')

class TestAcquisitionPipeline(unittest.TestCase):
    def test_successful_html_result_contract(self):
        from unittest.mock import patch
        html=('<html lang="en"><head><meta property="article:published_time" content="2026-11-21T12:00:00Z"></head><body><article><h1>AIVOYA engineering validates repeatable acquisition</h1><p>'+('A traceable source report with deterministic capture and reliable content validation. '*14)+'</p></article></body></html>').encode()
        job={'schema_version':'1.0','job_id':'job_test','lease_token':'lease_test','url':'https://example.com/news/article-one','method':'html_static','rights':'full_text_licensed'}
        with patch('aivoya_worker.fetch.enforce',return_value=job['url']),patch('aivoya_worker.fetch.controlled_get',return_value=(html,200,job['url'],'text/html')):
            result=execute(job,engine='stdlib')
        self.assertEqual(result['status'],'success')
        self.assertEqual(result['job_id'],'job_test')
        self.assertGreater(len(result['body_text']),300)
        self.assertEqual(result['title'],'AIVOYA engineering validates repeatable acquisition')
        import base64,hashlib
        self.assertEqual(base64.b64decode(result['raw_base64']),html)
    def test_rss_discovery_result_contract(self):
        from unittest.mock import patch
        rss=b'<rss><channel><item><title>Grounded research report</title><link>https://example.com/news/grounded-report</link></item></channel></rss>'
        job={'schema_version':'1.0','job_id':'job_rss','lease_token':'lease_rss','url':'https://example.com/feed.xml','method':'rss','rights':'metadata_only'}
        with patch('aivoya_worker.fetch.enforce',return_value=job['url']),patch('aivoya_worker.fetch.controlled_get',return_value=(rss,200,job['url'],'application/rss+xml')):
            result=execute(job,engine='stdlib')
        self.assertEqual(result['status'],'success')
        self.assertEqual(len(result['entries']),1)
        self.assertEqual(result['entries'][0]['url'],'https://example.com/news/grounded-report')
