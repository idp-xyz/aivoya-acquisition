#!/usr/bin/env python3
"""Opt-in CI-only live adapter smoke tests with explicit target restrictions.

No anti-bot bypass, no authenticated site scraping, no article redistribution.
Outputs a machine-readable attestation; an exception fails CI (not an imaginary pass).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'worker'))


def check_github_api() -> dict:
    """Run the actual worker, including robots/rights policy, against official public metadata."""
    from aivoya_worker.fetch import execute

    url = 'https://api.github.com/repos/D4Vinci/Scrapling/releases?per_page=3'
    job = {
        'schema_version': '1.0',
        'job_id': 'ci-github-official-api-smoke',
        'lease_token': 'local-ci-test-lease',
        'url': url,
        'origin_url': 'https://github.com/D4Vinci/Scrapling',
        'method': 'github_releases',
        'rights': 'metadata_only',
    }
    result = execute(job, engine='stdlib')
    if result.get('status') != 'success':
        raise AssertionError('Real worker returned %s: %s (%s)' % (
            result.get('status'), result.get('error_code'), result.get('error')))
    entries = result.get('entries') or []
    if not entries:
        raise AssertionError('No structured GitHub release entries returned')
    if any(not e.get('url', '').startswith('https://github.com/D4Vinci/Scrapling/releases/') for e in entries):
        raise AssertionError('Out-of-scope release URL in result')
    return {
        'target': url,
        'http_status': result.get('http_status'),
        'entries': len(entries),
        'extractor': result.get('extractor_name'),
        'duration_ms': result.get('duration_ms'),
        'scope': 'public release metadata only',
    }


def check_scrapling_http() -> dict:
    """Optional, one-request real Internet test; no stealth or anti-bot workaround."""
    from scrapling.fetchers import Fetcher
    url = 'https://example.com/'
    page = Fetcher.get(url, timeout=25)
    text = page.css('h1::text').get()
    if int(page.status) != 200 or not text:
        raise AssertionError('Expected HTTP 200 with a nonempty H1 from example.com')
    return {'target': url, 'http_status': int(page.status), 'h1_detected': True}


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'''<!doctype html><html><head><title>CI Fixture</title></head>
<body><main id="mount"></main><script>
document.getElementById('mount').innerHTML = '<h1 id="rendered">JS rendered fixture</h1>';
</script></body></html>'''
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, fmt, *args):
        pass


def check_scrapling_browser() -> dict:
    """Test JavaScript rendering against an isolated localhost fixture, not arbitrary sites."""
    from scrapling.fetchers import StealthyFetcher
    server = ThreadingHTTPServer(('127.0.0.1', 0), _FixtureHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = 'http://127.0.0.1:%d/' % server.server_port
    try:
        page = StealthyFetcher.fetch(url, headless=True, solve_cloudflare=False)
        h1 = page.css('#rendered::text').get()
        if h1 != 'JS rendered fixture':
            raise AssertionError('Browser did not render JavaScript fixture; got %r' % h1)
        return {'target': 'localhost:ephemeral', 'status': int(page.status), 'javascript_rendered': True}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


STAGES = {
    'github-api': check_github_api,
    'scrapling-http': check_scrapling_http,
    'scrapling-browser': check_scrapling_browser,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=[*STAGES, 'all'], required=True)
    parser.add_argument('--out', default='artifacts/cloud-smoke.json')
    args = parser.parse_args()
    stages = list(STAGES) if args.stage == 'all' else [args.stage]
    document = {
        'system': 'AIVOYA Acquisition Fabric V1.1',
        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
        'environment': {'ci': os.getenv('CI', 'false'), 'python': sys.version.split()[0]},
        'results': [],
        'overall': 'passed',
    }
    for name in stages:
        entry = {'stage': name, 'started_utc': datetime.now(timezone.utc).isoformat()}
        start = time.monotonic()
        try:
            entry['evidence'] = STAGES[name]()
            entry['status'] = 'passed'
        except Exception as exc:
            entry['status'] = 'failed'
            entry['error'] = str(exc)[:2000]
            entry['exception_type'] = type(exc).__name__
            document['overall'] = 'failed'
            traceback.print_exc()
        entry['elapsed_ms'] = round((time.monotonic() - start) * 1000)
        document['results'].append(entry)
        print('%s: %s' % (name, entry['status']))
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + '\n', encoding='utf8')
    print('Evidence JSON:', path)
    return 0 if document['overall'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
