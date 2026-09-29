"""Every local page gets the same navigation; briefs retain script isolation."""
import base64
import hashlib
from html.parser import HTMLParser
from uuid import uuid4

import pytest

from api.ui import PAGES

# split part 2: the read-only routes now need a credential like every route.
READER = {"Authorization": "Bearer test-token"}


class Shell(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.in_nav = False
        self.frames = []
        self.script = ''
        self.in_script = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'nav' and attrs.get('aria-label') == 'Main navigation':
            self.in_nav = True
        if tag == 'a' and self.in_nav:
            self.links.append(attrs)
        if tag == 'iframe':
            self.frames.append(attrs)
        if tag == 'script':
            self.in_script = True

    def handle_endtag(self, tag):
        if tag == 'nav':
            self.in_nav = False
        if tag == 'script':
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.script += data


@pytest.mark.parametrize('path', [page[0] for page in PAGES])
def test_all_pages_have_complete_navigation_and_one_active_destination(client, path):
    response = client.get(path, headers=READER)
    assert response.status_code == 200
    page = Shell(response.text)
    assert [a['href'] for a in page.links] == [p[0] for p in PAGES]
    assert [a['href'] for a in page.links if a.get('aria-current') == 'page'] == [path]
    assert 'id="page-content"' in response.text
    assert 'id="menu-toggle"' in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert 'test-token' not in response.text


def test_brief_shell_script_is_hash_bound_and_artifact_stays_isolated(client):
    headers = {'Authorization': 'Bearer test-token'}
    response = client.post('/artifacts', headers=headers, json={
        'request_id': str(uuid4()), 'kind': 'daily', 'actor': 'Test', 'note': 'Synthetic',
        'document': {'title': 'A <script> is text', 'html': '<p>Safe document</p>',
                     'period_start': '2026-09-23T00:00:00-04:00', 'as_of': '2026-09-23T09:00:00-04:00',
                     'coverage': [{'source': 'gmail', 'status': 'unavailable', 'detail': 'Synthetic'}]}})
    assert response.status_code == 200
    response = client.get('/ui/briefs/' + response.json()['id'] + '?revision=1', headers=READER)
    page = Shell(response.text)
    assert [a['href'] for a in page.links if a.get('aria-current')] == ['/ui/briefs']
    assert 'A &lt;script&gt; is text' in response.text
    policy = response.headers['content-security-policy']
    hashed = base64.b64encode(hashlib.sha256(page.script.encode()).digest()).decode()
    assert f"script-src 'sha256-{hashed}'" in policy
    assert "script-src 'unsafe-inline'" not in policy
    assert len(page.frames) == 1 and page.frames[0]['sandbox'] == ''
    inner = page.frames[0]['srcdoc']
    assert "default-src 'none'" in inner and '<script' not in inner
    assert 'app-navigation' not in inner
