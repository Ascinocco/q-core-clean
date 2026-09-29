"""Shared local web shell; one navigation registry and dark design system."""
import base64
import hashlib
from pathlib import Path

from fastapi.responses import HTMLResponse

STATIC = Path(__file__).parent / 'static'
PAGES = (
    ('/ui/spending', 'Spending', 'Understand your spending'),
    ('/ui/forecast', 'Cash runway', 'Plan upcoming cash movements'),
    ('/ui/evals', 'Financial evaluations', 'Review published results'),
    ('/ui/briefs', 'Daily briefs', 'Daily and weekly snapshots'),
    ('/ui/tokens', 'Access tokens', 'Create and revoke machine tokens'),
)


def stylesheet():
    return (STATIC / 'shell.css').read_text()


def shell(active):
    links = ''.join(
        f'<a href="{path}"' + (' aria-current="page"' if path == active else '')
        + f'><span>{label}</span><small>{detail}</small></a>'
        for path, label, detail in PAGES
    )
    label = next(label for path, label, _ in PAGES if path == active)
    return f'''<a class="skip-link" href="#page-content">Skip to content</a>
<header class="app-bar"><button class="menu-toggle" id="menu-toggle" type="button" aria-label="Open navigation" aria-controls="app-navigation" aria-expanded="false"><span aria-hidden="true">☰</span></button><a class="app-brand" href="/ui/briefs">q-core</a><span class="app-location">{label}</span></header>
<dialog class="app-drawer" id="app-navigation" aria-labelledby="navigation-title"><div class="drawer-heading"><h2 id="navigation-title">q-core</h2><button id="menu-close" type="button" aria-label="Close navigation" autofocus>✕</button></div><p class="drawer-label">Your workspace</p><nav aria-label="Main navigation">{links}</nav><p class="drawer-footer">Your life, in one place.</p></dialog>'''


def render_page(html, active, *, isolated=False):
    """Only the fixed shell script is allowed on the artifact outer page.

    Artifact content still lives in an empty sandbox with its own no-script
    policy. Dynamic source text is never interpolated into this trusted script.
    """
    script = (STATIC / 'shell.js').read_text()
    html = html.replace('</head>', '<link rel="icon" href="data:,"></head>', 1) if 'rel="icon"' not in html else html
    html = html.replace('</head>', f'<style>{stylesheet()}</style></head>', 1)
    html = html.replace('<!-- app-shell -->', shell(active) + f'<script>{script}</script>', 1)
    headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
               'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY'}
    if isolated:
        sha = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        headers['Content-Security-Policy'] = (
            f"default-src 'none'; script-src 'sha256-{sha}'; style-src 'unsafe-inline'; "
            "img-src data:; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
    return HTMLResponse(html, headers=headers)


def static_page(filename, active):
    # Filename comes from route code, never from a request.
    return render_page((STATIC / filename).read_text(), active)
