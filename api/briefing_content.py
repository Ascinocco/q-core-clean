"""Static HTML allowlist and server-side personal-data screening.

No scripts, CSS, links, images, SVG, forms or embeds. Static diagrams use pre.
Never pass untrusted content to a template or execute it. This is deliberately
smaller than a browser sanitizer: every tag and attribute is refused by default.
"""
from html import escape
from html.parser import HTMLParser
import unicodedata

from fastapi.exceptions import RequestValidationError
from api.personal_redaction import load_profile, scrub_personal
from api.privacy import refuse_sensitive_numbers

TAGS = frozenset('h1 h2 h3 h4 p ul ol li strong em b i table thead tbody tr th td caption blockquote pre code hr br section div span'.split())
VOID = frozenset({'br', 'hr'})


def clean_text(text, profile):
    value = unicodedata.normalize('NFKC', text)
    value = ''.join(c for c in value if unicodedata.category(c) != 'Cf')
    refuse_sensitive_numbers(value)
    return scrub_personal(value, profile).text


class StaticDocument(HTMLParser):
    def __init__(self, profile):
        super().__init__(convert_charrefs=True)
        self.profile = profile
        self.parts = []
        self.stack = []
        self.visible = []

    def handle_starttag(self, tag, attrs):
        if tag not in TAGS or attrs:
            raise ValueError('Only static document tags without attributes are supported')
        if len(self.stack) >= 40:
            raise ValueError('Document nesting exceeds limit')
        self.parts.append(f'<{tag}>')
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError('Document tags must be balanced')
        self.parts.append(f'</{tag}>')

    def handle_startendtag(self, tag, attrs):
        if tag not in VOID:
            raise ValueError('Only br and hr can be self closing')
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        cleaned = clean_text(data, self.profile)
        self.visible.append(cleaned)
        self.parts.append(escape(cleaned))

    def handle_comment(self, data):
        raise ValueError('HTML comments are not supported')

    def handle_decl(self, decl):
        raise ValueError('Use an HTML fragment, not a full HTML document')

    def handle_pi(self, data):
        raise ValueError('Processing instructions are not supported')

    def unknown_decl(self, data):
        raise ValueError('HTML declarations are not supported')

    def finish(self, content):
        self.feed(content)
        self.close()
        if self.stack or not ''.join(self.visible).strip():
            raise ValueError('Document must contain text and balanced tags')
        # Catch identifiers split over tags, even when each fragment is innocuous.
        joined = ''.join(self.visible)
        if clean_text(joined, self.profile) != joined:
            raise ValueError('Personal information must not be split across tags')
        return ''.join(self.parts)


BRIEF_REFUSAL = 'Content refused: use privacy-safe summaries and balanced static HTML without attributes, links or active content'


def screen_payload(value, settings, *, html=None, refusal=None, detailed=False):
    """Screen every string in a request payload before it is stored.

    `html(text, profile)` handles any value under an `html` key: the caller
    passes its kind's screen (api.artifact_kinds), so brief rules are never
    applied to another kind by default. Without it, html is ordinary text.
    """
    from api.artifact_kinds import is_provenance_sha
    profile = load_profile(settings.privacy_profile_path)
    def walk(item, path='', key=''):
        if isinstance(item, str):
            if key == 'html' and html is not None:
                return html(item, profile)
            if key in {'reference', 'source_key', 'request_id'}:
                # References must not be a route around identifier screening.
                refuse_sensitive_numbers(item)
                return item
            if is_provenance_sha(path, item):
                return item
            return clean_text(item, profile)
        if isinstance(item, list):
            return [walk(v, f'{path}[{i}]') for i, v in enumerate(item)]
        if isinstance(item, dict):
            return {k: walk(v, f'{path}.{k}' if path else k, k) for k, v in item.items()}
        return item
    try:
        return walk(value)
    except ValueError as error:
        message = refusal or BRIEF_REFUSAL
        if detailed:
            message = f'{message}: {error}'
        raise RequestValidationError([{'loc': ('body', 'content'), 'msg': message, 'type': 'value_error'}]) from None
