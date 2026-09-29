"""Per-kind rules for artifacts: document model, screening and viewer route.

The handlers in `api.artifacts` look a kind up here and never branch on its
name. Briefs are registered from the start. A4 registers `page` and A7
registers `diagram`. A kind the schema allows but nothing has registered is
refused with a 422 rather than stored under the wrong rules.
"""
from dataclasses import dataclass
import re
from typing import Callable

from pydantic import BaseModel

from api.artifact_models import SHA_PATTERN
from api.briefing_content import BRIEF_REFUSAL, StaticDocument
from api.briefing_models import Document
from api.config import Settings
from api.personal_redaction import PrivacyProfile


@dataclass(frozen=True)
class KindRules:
    document_model: type[BaseModel]
    refusal: str                                     # 422 message for a refused document
    viewer_path: Callable[[str], str]
    screen_html: Callable[[str, PrivacyProfile], str] | None = None      # html kinds: returns stored html or raises ValueError
    screen_document: Callable[[dict, Settings], dict] | None = None      # structured kinds: screens the whole document (A7's screen_diagram)
    detailed_errors: bool = False                    # A4 sets True for pages: the 422 carries the specific reason


def screen_brief_html(html: str, profile: PrivacyProfile) -> str:
    return StaticDocument(profile).finish(html)


BRIEF = KindRules(document_model=Document, refusal=BRIEF_REFUSAL,
                  viewer_path=lambda identifier: f'/ui/briefs/{identifier}',
                  screen_html=screen_brief_html)

BRIEF_KINDS = frozenset({'daily', 'weekly'})
KINDS: dict[str, KindRules] = {'daily': BRIEF, 'weekly': BRIEF}
ALL_KINDS = ('daily', 'weekly', 'page', 'diagram')


def viewer_path(kind: str, identifier: str) -> str:
    rules = KINDS.get(kind)
    # Page and diagram rows open in the Canvas viewer (A5) even before their
    # kind is registered, so a directly inserted row still gets a real route.
    return rules.viewer_path(identifier) if rules else f'/ui/canvas/{identifier}'


_PROVENANCE_SHA_PATH = re.compile(r'document\.provenance\[\d+\]\.sha')


def is_provenance_sha(path: str, value: str) -> bool:
    """True only for a well-formed full SHA at document.provenance[i].sha.

    A 40-hex SHA usually contains a run of seven or more digits, which the
    privacy screen refuses as a possible account number. Such a value skips
    that screen, and nothing else does: a `sha` key anywhere else, or a
    malformed SHA, is screened as usual (decisions-log, 2026-09-26).
    """
    return (isinstance(value, str) and _PROVENANCE_SHA_PATH.fullmatch(path) is not None
            and re.fullmatch(SHA_PATTERN, value) is not None)


# Registered last: api.canvas_diagram imports this module's helpers at call time.
from api.canvas_diagram import DiagramDocument, screen_diagram  # noqa: E402

KINDS['diagram'] = KindRules(document_model=DiagramDocument, screen_document=screen_diagram,
                             refusal='Diagram refused', viewer_path=lambda i: f'/ui/canvas/{i}',
                             detailed_errors=True)
