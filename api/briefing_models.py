"""Small document/inbox contracts. HTML is a static fragment, never an app."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, model_validator

from api.models import KeyDate
from api.privacy import StoredText


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Source(Model):
    provider: Literal['q_core', 'gmail', 'google_calendar', 'operator']
    # Opaque references are data, not URLs to fetch. UUID/hex sources may contain digits.
    reference: str = Field(min_length=1, max_length=250, pattern=r'^[a-zA-Z0-9_:./@-]+$')
    summary: StoredText = Field(min_length=1, max_length=1000)


class Coverage(Model):
    source: Literal['gmail', 'google_calendar', 'reminders', 'forecast', 'transactions', 'review_inbox']
    status: Literal['complete', 'partial', 'unavailable']
    detail: StoredText = Field(min_length=1, max_length=1000)


class Document(Model):
    format: Literal['static_html_v1'] = 'static_html_v1'
    title: StoredText = Field(min_length=1, max_length=180)
    html: str = Field(min_length=1, max_length=200000)
    period_start: AwareDatetime
    as_of: AwareDatetime
    timezone: Literal['America/New_York'] = 'America/New_York'
    sources: list[Source] = Field(default_factory=list, max_length=200)
    coverage: list[Coverage] = Field(min_length=1, max_length=6)

    @model_validator(mode='after')
    def valid_window(self):
        if self.period_start > self.as_of or (self.as_of - self.period_start).days > 31:
            raise ValueError('Report window must be ordered and at most 31 days')
        if len({c.source for c in self.coverage}) != len(self.coverage):
            raise ValueError('Coverage sources must be unique')
        return self


# The document is validated in the handler against its kind's model
# (api.artifact_kinds.KINDS), which a static union here could not express.
class ArtifactCreate(Model):
    request_id: UUID
    kind: Literal['daily', 'weekly', 'page', 'diagram']
    document: dict
    actor: StoredText = Field(min_length=1, max_length=100)
    note: StoredText = Field(min_length=1, max_length=700)


class ArtifactUpdate(Model):
    expected_revision: StrictInt = Field(ge=1)
    document: dict  # validated against the stored kind's model
    actor: StoredText = Field(min_length=1, max_length=100)
    note: StoredText = Field(min_length=1, max_length=700)


class ReviewContent(Model):
    summary: StoredText = Field(min_length=1, max_length=2000)
    sources: list[Source] = Field(default_factory=list, max_length=20)


class ReviewCreate(Model):
    source_key: UUID
    content: ReviewContent
    actor: StoredText = Field(min_length=1, max_length=100)
    note: StoredText = Field(min_length=1, max_length=700)


class ReviewUpdate(Model):
    expected_revision: StrictInt = Field(ge=1)
    content: ReviewContent
    state: Literal['open', 'deferred', 'resolved', 'dismissed']
    revisit_date: KeyDate | None = None
    actor: StoredText = Field(min_length=1, max_length=100)
    note: StoredText = Field(min_length=1, max_length=700)

    @model_validator(mode='after')
    def deferral(self):
        if (self.state == 'deferred') != (self.revisit_date is not None):
            raise ValueError('Only deferred items require a revisit date')
        return self
