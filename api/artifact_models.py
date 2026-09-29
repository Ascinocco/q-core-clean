"""Canvas document contracts shared by every non-brief artifact kind.

A1 defines these but registers none of them: A4 registers `PageDocument` as
the `page` kind and A7 builds `DiagramDocument(ArtifactMeta)` for `diagram`.
Brief models stay in `api.briefing_models` and are re-exported here.
"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator, field_validator

from api.briefing_models import ArtifactCreate, ArtifactUpdate, Document, Model
from api.privacy import StoredText

__all__ = ['ArtifactCreate', 'ArtifactEdit', 'ArtifactUpdate', 'ArtifactMeta', 'Document', 'PageDocument', 'Provenance',
           'Replacement', 'SHA_PATTERN']

SHA_PATTERN = r'^[0-9a-f]{40}$'

Tag = Annotated[str, Field(pattern=r'^[a-z0-9][a-z0-9-]{0,31}$')]
RelativePath = Annotated[str, Field(min_length=1, max_length=200)]


class Provenance(Model):
    repo: str = Field(pattern=r'^[a-z0-9][a-z0-9._-]{0,62}$')
    # The full SHA from `git rev-parse HEAD`. Viewers show the first seven.
    sha: str = Field(pattern=SHA_PATTERN)
    paths: list[RelativePath] = Field(default_factory=list, max_length=50)

    @field_validator('paths')
    @classmethod
    def relative_posix(cls, paths):
        for path in paths:
            if path.startswith('/') or '\\' in path or '..' in path:
                raise ValueError('Paths must be relative POSIX paths without ..')
        return paths


class ArtifactMeta(Model):
    title: StoredText = Field(min_length=1, max_length=180)
    tags: list[Tag] = Field(default_factory=list, max_length=20)
    provenance: list[Provenance] = Field(default_factory=list, max_length=20)

    @field_validator('tags')
    @classmethod
    def unique_tags(cls, tags):
        if len(set(tags)) != len(tags):
            raise ValueError('Tags must be unique')
        return tags


class PageDocument(ArtifactMeta):
    format: Literal['canvas.page/v1']
    html: str = Field(min_length=1, max_length=500000)


class Replacement(BaseModel):
    # Not Model: whitespace is part of the text being matched and inserted.
    model_config = ConfigDict(extra='forbid')
    old: str = Field(min_length=1, max_length=20000)
    new: str = Field(max_length=200000)


class ArtifactEdit(Model):
    """A targeted edit, applied to the stored revision `expected_revision`.

    `replacements` are applied in order to `document.html`. `title`, `tags`
    and `provenance` replace the whole field; the kind's model decides
    whether it has them (briefs have no tags or provenance).
    """
    expected_revision: StrictInt = Field(ge=1)
    replacements: list[Replacement] = Field(default_factory=list, max_length=50)
    title: StoredText | None = Field(None, min_length=1, max_length=180)
    tags: list[Tag] | None = Field(None, max_length=20)
    provenance: list[Provenance] | None = Field(None, max_length=20)
    actor: StoredText = Field(min_length=1, max_length=100)
    note: StoredText = Field(min_length=1, max_length=700)

    @model_validator(mode='after')
    def something_to_do(self):
        if not self.replacements and self.title is None and self.tags is None and self.provenance is None:
            raise ValueError('An edit needs replacements, title, tags or provenance')
        return self
