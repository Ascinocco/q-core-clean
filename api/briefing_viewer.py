"""Local read-only collection. No credentials or generated scripts in the browser."""
from html import escape
from datetime import datetime
import sqlite3
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query

from api.auth import require_reader
from api.artifact_kinds import BRIEF_KINDS
from api.artifacts import artifact, artifact_rows
from api.db import get_connection
from api.errors import NotFoundError
from api.ui import render_page, stylesheet

router = APIRouter()

STYLE = '.card{margin:20px 0}.meta{font-size:.9rem}iframe{display:block;width:100%;height:75vh;border:1px solid var(--ring);border-radius:12px;background:var(--surface);margin:24px 0}.empty{padding:40px 0}pre{white-space:pre-wrap;padding:16px;background:var(--wash);overflow-wrap:anywhere}table{border-collapse:collapse;width:100%;margin:20px 0}th,td{padding:12px;text-align:left;border-bottom:1px solid var(--ring)}'

def stamp(value):
    return datetime.fromisoformat(value).astimezone(ZoneInfo('America/New_York')).strftime('%b %d, %Y · %I:%M %p %Z')


def page(title, body):
    return render_page('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                       f'<title>{escape(title)} · q-core</title><style>{STYLE}</style></head><body><!-- app-shell --><main id="page-content" class="app-content" tabindex="-1">{body}</main></body></html>', '/ui/briefs', isolated=True)


@router.get('/ui/briefs', include_in_schema=False, dependencies=[Depends(require_reader)])
def briefs(kind: Literal['daily', 'weekly'] | None = None, offset: int = Query(0, ge=0),
           connection: sqlite3.Connection = Depends(get_connection)):
    result = artifact_rows(connection, kind=kind, kinds=tuple(sorted(BRIEF_KINDS)), limit=20, offset=offset)
    body = '<p class="meta">Q-CORE / YOUR SAVED REPORTS</p><h1>Daily briefs</h1><p>Daily and weekly snapshots, created with Claude.</p><nav aria-label="Brief filters"><a href="/ui/briefs">All</a><a href="/ui/briefs?kind=daily">Daily</a><a href="/ui/briefs?kind=weekly">Weekly</a></nav>'
    if not result['items']:
        body += '<p class="empty">No briefs here yet. Ask Claude to create your daily brief or week-to-date report.</p>'
    for row in result['items']:
        body += f'<article class="card"><p class="meta">{escape(row["kind"].title())} · {stamp(row["created_at"])}</p><h2><a href="/ui/briefs/{row["id"]}">{escape(row["title"])}</a></h2><p class="meta">Revision {row["revision"]} · Updated {stamp(row["updated_at"])}</p></article>'
    query = '' if kind is None else '&kind='+kind
    body += '<nav>'
    if offset:
        body += f'<a href="/ui/briefs?offset={max(0,offset-20)}{query}">Newer briefs</a>'
    if offset + 20 < result['total']:
        body += f'<a href="/ui/briefs?offset={offset+20}{query}">Older briefs</a>'
    body += '</nav><footer>Sorted by creation date. Revisions keep their original place.</footer>'
    return page('Daily briefs', body)


@router.get('/ui/briefs/{artifact_id}', include_in_schema=False, dependencies=[Depends(require_reader)])
def brief(artifact_id: UUID, revision: int | None = Query(None, ge=1),
          connection: sqlite3.Connection = Depends(get_connection)):
    value = artifact(connection, artifact_id, revision)
    if value['kind'] not in BRIEF_KINDS:
        # Other kinds have their own viewer and none of a brief's fields.
        raise NotFoundError('Artifact not found')
    doc = value['document']
    title = escape(doc['title'])
    body = f'<nav><a href="/ui/briefs">← Daily briefs</a></nav><h1>{title}</h1><p class="meta">{value["kind"].title()} · Created {stamp(value["created_at"])} · Revision {value["revision"]} of {value["current_revision"]}</p>'
    body += f'<p class="meta">Report period: {stamp(doc["period_start"])} to {stamp(doc["as_of"])}. This is a saved snapshot.</p>'
    inner = f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'"><style>{stylesheet()}{STYLE}html{{background:var(--surface)}}body{{background:var(--surface);padding:24px;min-height:100vh;overflow-wrap:anywhere}}</style></head><body>{doc["html"]}</body></html>'
    # Empty sandbox grants no scripts, forms, same-origin access or navigation.
    body += f'<iframe sandbox="" title="Brief document" srcdoc="{escape(inner, quote=True)}"></iframe>'
    body += '<section class="card"><h2>Source coverage</h2><ul>'
    for c in doc['coverage']:
        body += f'<li><strong>{escape(c["source"])}</strong> · {escape(c["status"])} — {escape(c["detail"])}</li>'
    body += '</ul>'
    if doc['sources']:
        body += '<h3>References</h3><ul>'
        for source in doc['sources']:
            body += f'<li>{escape(source["provider"])} · {escape(source["reference"])} — {escape(source["summary"])}</li>'
        body += '</ul>'
    body += '</section><nav>'
    if value['revision'] > 1:
        body += f'<a href="/ui/briefs/{artifact_id}?revision={value["revision"]-1}">Earlier revision</a>'
    if value['revision'] < value['current_revision']:
        body += f'<a href="/ui/briefs/{artifact_id}?revision={value["revision"]+1}">Next revision</a><a href="/ui/briefs/{artifact_id}">Latest revision</a>'
    body += '</nav><footer>Ask Claude to revise this brief or review your current inbox.</footer>'
    return page(doc['title'], body)
