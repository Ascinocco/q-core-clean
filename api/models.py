import re
from datetime import date, datetime, time
from typing import Annotated, ClassVar, Literal, get_args
from uuid import UUID

from fastapi.exceptions import RequestValidationError
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
)

from api.errors import InvalidReferenceError
from api.privacy import StoredText, refuse_sensitive_numbers_in


class Address(BaseModel):
    model_config = ConfigDict(extra="forbid")

    street: StoredText | None = None
    city: StoredText | None = None
    state: StoredText | None = None
    zip: StoredText | None = None


class Mortgage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lender: StoredText | None = None
    origination_date: date | None = None
    term_years: int | None = None
    rate: float | None = None


class PersonAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_of_birth: date | None = None
    birthday_reminder_enabled: bool | None = None
    relationship_to_you: (
        Literal["self", "spouse", "child", "dependent", "tenant", "other"] | None
    ) = None
    # Capped so a full SSN can't be persisted even if one is sent. CLAUDE.md
    # assigns redaction to the intake-skill layer, but api/ owns the DB and is
    # the last gate before a write. Rejects rather than truncates: silently
    # storing the last 4 of a full SSN would hide the upstream leak.
    id_last4: str | None = Field(default=None, max_length=4)


class PropertyAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: Address | None = None
    property_type: (
        Literal["single_family", "condo", "land", "rental", "multi_family"] | None
    ) = None
    purchase_date: date | None = None
    purchase_price: float | None = None
    year_built: int | None = None
    square_footage: int | None = None
    mortgage: Mortgage | None = None
    # Forward-dated, unlike purchase_date: these are what the "what's due"
    # digest reads for a property. Each holds the NEXT occurrence, not a
    # history — once the date passes it is stale until something rolls it
    # forward, which is why /due surfaces a past one as overdue rather
    # than filtering it out (D17).
    insurance_renewal: date | None = None
    tax_due: date | None = None


class VehicleAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vin: StoredText | None = None
    make: StoredText | None = None
    model: StoredText | None = None
    year: int | None = None
    license_plate: StoredText | None = None
    purchase_date: date | None = None
    purchase_price: float | None = None
    # See the note on PropertyAttributes. registration_expiry reverses the
    # attribute runbook's earlier "that's a reminders row, not a
    # duplicated fact" — it is not duplicated while no reminders row
    # exists, and keeping it on the vehicle means the digest derives the
    # date from the thing itself instead of from a parallel record that
    # can drift (D17).
    registration_expiry: date | None = None
    warranty_end: date | None = None


class PetAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    species: StoredText | None = None
    breed: StoredText | None = None
    date_of_birth: date | None = None
    birthday_reminder_enabled: bool | None = None
    microchip_id: StoredText | None = None
    # See the note on PropertyAttributes.
    vaccination_due: date | None = None


class ProjectAttributes(BaseModel):
    """A project is an entity, not its own table — see the design's
    "generalize, don't proliferate tables" call. Title maps to entities.name,
    with an optional association to a managed software repository."""

    model_config = ConfigDict(extra="forbid")

    description: StoredText | None = None
    repository_path: str | None = Field(
        default=None,
        pattern=r"^projects/[a-z0-9]+(?:-[a-z0-9]+)*$",
        description="Canonical repository-relative submodule path, projects/<lowercase-slug>.",
    )


class AccountAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    institution: StoredText | None = None
    account_subtype: (
        Literal["checking", "savings", "credit_card", "loan", "investment", "insurance"]
        | None
    ) = None
    # See id_last4 on PersonAttributes — same reasoning for account numbers.
    last4: str | None = Field(default=None, max_length=4)
    opened_date: date | None = None
    interest_rate: float | None = None
    policy_type: (
        Literal["homeowners", "auto", "umbrella", "life", "health"] | None
    ) = None
    premium: float | None = None
    renewal_date: date | None = None


# Which date-typed attributes point forward. Nearly all of them are
# historical — purchase_date, opened_date, date_of_birth record something
# that already happened, and a "what's due" query has no use for them.
# These are the exceptions, and they are the complete set the /due digest
# reads from entity attributes (D17). Each holds the NEXT occurrence, not
# a series: once the date passes the value is stale until something rolls
# it forward, so /due reports a past one as overdue rather than hiding
# it. Anything with a real recurrence rule belongs in `reminders`.
#
# This lives beside the models rather than in the digest so that the two
# cannot drift: test_attribute_dates_are_all_classified fails if a date
# field is added to any attribute model without being placed here or in
# HISTORICAL_DATE_ATTRIBUTES. A field that is in neither would otherwise
# be invisible to "what's due" with nothing to say so.
#
# ONE DIRECTION IS DELIBERATELY UNGUARDED, and it is worth knowing which
# (found by review-1 on #78). The tests here catch a date field on
# NEITHER list and a name on a list that is not a real date field. They
# cannot catch a field on the WRONG list — moving `registration_expiry`
# into HISTORICAL_DATE_ATTRIBUTES leaves both lists well-formed and
# every test here green, while the symptom is exactly the one this
# comment warns about: a renewal that never appears, reported as
# "nothing is due". No guard is possible at this layer, because which
# side a date belongs on is a judgement about what the date MEANS, not
# a property of the model. The check that does catch it lives on the
# consumer side: api/tests/test_due.py asserts that each field named
# here actually surfaces from GET /due for a seeded entity, with its
# expected list written out literally rather than derived from this one
# — a derived list would follow the field to the wrong side and pass.
FORWARD_DATED_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "property": ("insurance_renewal", "tax_due"),
    "vehicle": ("registration_expiry", "warranty_end"),
    "pet": ("vaccination_due",),
    "account": ("renewal_date",),
}

# The other half of the same partition; see above. Listed explicitly
# rather than inferred as "everything not forward-dated", because that
# would make the classification test vacuous — every new field would
# classify itself as historical by default.
HISTORICAL_DATE_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "person": ("date_of_birth",),
    "property": ("purchase_date",),
    "vehicle": ("purchase_date",),
    "pet": ("date_of_birth",),
    "account": ("opened_date",),
}


ATTRIBUTE_MODELS: dict[str, type[BaseModel]] = {
    "person": PersonAttributes,
    "property": PropertyAttributes,
    "vehicle": VehicleAttributes,
    "pet": PetAttributes,
    "account": AccountAttributes,
    "project": ProjectAttributes,
}


# Attribute fields whose rejected value is scrubbed from the error object.
# Pydantic reports the offending value as the error's `input`.
#
# THIS COMMENT USED TO SAY that error list "becomes the 422 body — which
# reaches the MCP client and so a Claude session's context". It does not.
# `api/main.py`'s `_error_detail` projects exactly two keys, `field` and
# `message`, and drops `input` and `url` for every error type. Measured
# end-to-end: a caller-invented key holding a 9-digit run comes back
# nowhere in the response, and a handled validation error is logged
# through the `logger.info` branch with no error detail either.
#
# The false version cost real time. I read it, measured only this layer,
# found that an UNRECOGNISED key is never matched here, and reported a
# live leak on the strength of a comment that had never been checked
# against what ships (D119). The code and its tests agreed with each
# other; nothing compared either to the response.
#
# So this is DEFENCE IN DEPTH behind `_error_detail`, not the thing
# standing between a secret and an LLM. It still earns its place: it
# keeps the value out of the error object itself, which matters if a
# future handler ever renders `exc.errors()` without that projection.
# `api/tests/test_error_detail_drops_input.py` pins the layer that
# actually carries the guarantee.
SENSITIVE_ATTRIBUTE_FIELDS = {"last4", "id_last4"}


def _redact_sensitive_inputs(errors: list[dict]) -> list[dict]:
    return [
        {**error, "input": "[redacted]"}
        if SENSITIVE_ATTRIBUTE_FIELDS.intersection(error.get("loc", ()))
        else error
        for error in errors
    ]


def _name_permitted_keys(entity_type: str, errors: list[dict]) -> list[dict]:
    """Rewrite "Extra inputs are not permitted" to say what IS permitted.

    The rejection named the key it refused and nothing else, so a caller
    -- usually a model -- learned that `colour` is wrong without learning
    that the field is `model`. Every enum error in this codebase lists its
    legal values; an `extra="forbid"` rejection is the same kind of error
    and was the one place that did not.

    Derived from the model's own fields, never a hand-written list: the
    attribute models are where this is defined, and a second copy is the
    thing that goes stale (test_tools_vocabulary.py pins the two together).

    ALSO DROPS THE ECHOED INPUT, as defence in depth rather than as a
    fix for a live leak. `_redact_sensitive_inputs` masks values by KEY
    NAME, against a set of names known to be sensitive -- and an
    unrecognised key is by construction not on that list, so the error
    OBJECT holds it. It never reaches a response: `_error_detail` keeps
    only `field` and `message`. I reported that as a live leak without
    checking the layer downstream (D119, corrected).

    Dropping it here is still right, and cheap: for an `extra_forbidden`
    error the value carries no diagnostic weight -- the key is refused
    because it is unknown, not because of what it holds, and the caller
    knows what they sent.
    """
    permitted = ", ".join(sorted(ATTRIBUTE_MODELS[entity_type].model_fields))
    rewritten = []
    for error in errors:
        if error.get("type") != "extra_forbidden":
            rewritten.append(error)
            continue
        key = error.get("loc", ("?",))[-1]
        rewritten.append(
            {
                **error,
                "input": "[omitted]",
                "msg": (
                    f"'{key}' is not an attribute of a {entity_type}. "
                    f"Permitted attributes for {entity_type}: {permitted}."
                ),
            }
        )
    return rewritten


def validate_entity_attributes(entity_type: str, attributes: dict) -> dict:
    """Validate `attributes` against entity_type's shape (see
    runbooks/entity-attribute-schemas.md) and return the normalized dict.

    `entity_type` is assumed to already be one of ATTRIBUTE_MODELS' keys —
    callers only ever pass a value that already passed EntityCreate's
    Literal validation or was read back from a previously-created row.
    """
    model = ATTRIBUTE_MODELS[entity_type]
    try:
        validated = model.model_validate(attributes)
    except ValidationError as exc:
        # Two transforms, composed. Order doesn't matter:
        # _redact_sensitive_inputs matches on a set-intersection over the
        # whole `loc` tuple, so prefixing first leaves ("body",
        # "attributes", "last4") still matching {"last4", "id_last4"}.
        #
        # The re-prefix: Pydantic reports these errors relative to the
        # attribute model it validated against, so a bad
        # attributes.purchase_date would otherwise arrive as
        # ("purchase_date",) while a bad top-level field arrives as
        # ("body", "type") — indistinguishable, to a client, from a
        # top-level field named "purchase_date". This has to happen here
        # rather than in main.py's handler: by the time the handler sees the
        # error, the context that it came from the attributes blob is gone.
        raise RequestValidationError(
            _name_permitted_keys(
                entity_type,
                _redact_sensitive_inputs(
                    [
                        {**error, "loc": ("body", "attributes", *error["loc"])}
                        for error in exc.errors()
                    ]
                ),
            )
        ) from exc
    return validated.model_dump(mode="json", exclude_none=True)


EntityType = Literal[
    "person", "property", "vehicle", "pet", "account", "project"
]
EntityStatus = Literal[
    "active", "inactive", "sold", "totaled", "deceased", "closed", "archived"
]


class EntityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: EntityType
    name: StoredText
    status: EntityStatus = "active"
    attributes: dict = {}


def _require_at_least_one(model: BaseModel, *fields: str) -> None:
    """Refuse an update that names no field to change.

    An `*Update` model with every field optional accepts an empty body,
    and the endpoint then returns 200 with the unchanged row -- which
    reads exactly like a successful edit. The caller believes the
    correction landed; nothing changed.

    Third instance of this shape when it was finally swept:
    `MerchantRuleUpdate` at the API and again at the tool surface (#100),
    then `TransactionUpdate`, where it was worse than a no-op --
    `update_transaction` is the only writer of `edited_at`, so an empty
    PATCH stamped a row as hand-edited and #94's archive report then told
    the user a re-import would discard work nobody had done.

    A body that names a field with the value already stored IS an edit
    and is accepted. Re-asserting a value is a decision -- "yes, this is
    still right" -- and the caller said something. Only saying nothing is
    refused.

    `actor` and similar metadata are excluded by the caller passing only
    the settable fields: naming who is making a change is not a change.
    """
    if not (model.model_fields_set & set(fields)):
        listed = ", ".join(fields)
        raise ValueError(
            f"at least one of {listed} is required — "
            "an empty update would report success without changing anything"
        )


def cleared(model: BaseModel, field: str) -> bool:
    """Was `field` sent explicitly as null, rather than left out?

    The whole point of `model_fields_set`: pydantic records which keys the
    caller actually sent, so `{}` and `{"x": null}` stop being the same
    thing. Without it `None` means both "leave alone" and "unset", and a
    nullable field is write-once -- you can set it and never take it back
    (ticket T-17; impl-3 hit it first on a relationship's end_date).
    """
    return field in model.model_fields_set and getattr(model, field) is None


def resolve_patch(model: BaseModel, field: str, current):
    """The value a PATCH leaves in `field`, given what is stored now.

    Three cases, and the middle one is the whole ticket:

        absent          -> `current`  (leave alone)
        explicit null   -> None       (clear)
        a value         -> that value

    Written once rather than at each call site: the old spelling,
    `body.x if body.x is not None else row["x"]`, collapses the first two
    into "leave alone", which is how every nullable field became
    write-once.
    """
    if cleared(model, field):
        return None
    value = getattr(model, field)
    return value if value is not None else current


def _refuse_unclearable(model: BaseModel, *fields: str) -> None:
    """Refuse an explicit null on a column the database declares NOT NULL.

    Without this the clear reaches SQLite and returns an IntegrityError --
    a 500 on a request the caller got wrong and cannot diagnose from the
    response. Naming the field makes it a 422 they can act on.

    Each model declares CLEARABLE; api/tests/test_clearable_fields.py
    derives the real answer from the schema and fails when the two
    disagree, so a column changing nullability cannot silently desync.
    """
    unclearable = sorted(
        field for field in fields
        if cleared(model, field) and field not in model.CLEARABLE
    )
    if unclearable:
        clearable = ", ".join(sorted(model.CLEARABLE)) or "nothing on this model"
        raise ValueError(
            f"cannot clear {', '.join(unclearable)}: required, so there is no "
            f"value to fall back to -- send a replacement instead. "
            f"Clearable here: {clearable}"
        )


class EntityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    TABLE: ClassVar[str] = "entities"
    CLEARABLE: ClassVar[frozenset[str]] = frozenset(['attributes'])

    name: StoredText | None = None
    status: EntityStatus | None = None
    attributes: dict | None = None

    def model_post_init(self, __context) -> None:
        _require_at_least_one(self, "name", "status", "attributes")
        _refuse_unclearable(self, "name", "status", "attributes")


class EntityResponse(BaseModel):
    id: str
    type: EntityType
    name: str
    status: EntityStatus
    attributes: dict
    created_at: str
    updated_at: str | None = None


RelationshipType = Literal[
    "owns",
    "resides_at",
    "insures",
    "finances",
    "maintains",
    "leases",
    "spouse_of",
    "parent_of",
]

# Types expected to accumulate multiple rows over time (a lease renewal, a
# move to a new address) rather than being deduped on create — every other
# relationship_type is point-in-time and gets deduped in entities.py.
DATED_RELATIONSHIP_TYPES = {"leases", "resides_at"}

# Types where A->B and B->A state the same fact, so the row is stored once in
# whichever direction it was first recorded and read back from both sides
# (list_relationships already queries from_entity_id OR to_entity_id). Dedup
# for these has to match either orientation, or the same marriage recorded
# from each spouse's side lands twice. See the entity_relationships
# vocabulary table in runbooks/entity-attribute-schemas.md.
#
# Deliberately NOT parent_of: "A parent_of B" and "B parent_of A" are
# different claims and must both be able to exist.
SYMMETRIC_RELATIONSHIP_TYPES = {"spouse_of"}


class RelationshipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_entity_id: str
    relationship_type: RelationshipType
    start_date: date | None = None
    end_date: date | None = None
    attributes: dict = {}

    @field_validator("attributes")
    @classmethod
    def attributes_do_not_store_sensitive_numbers(cls, value: dict) -> dict:
        return refuse_sensitive_numbers_in(value)


class RelationshipUpdate(BaseModel):
    """Only the three fields that are not the relationship's identity.

    `from_entity_id`, `to_entity_id` and `relationship_type` are absent
    on purpose: they are the key `create_relationship` dedups on, so
    changing one would not amend this relationship, it would make it a
    different one. `extra="forbid"` turns an attempt into a 422 rather
    than a silent no-op.

    An explicit `null` CLEARS; an absent key leaves the stored value
    alone. Those are different requests -- see `cleared()`. Without the
    distinction `end_date` is write-once, which matters because D71 made
    `create_relationship` refuse a differing payload: a lease ended by
    mistake would otherwise be permanently ended, with delete-and-
    recreate the only escape and the id lost with it.

    This model shipped that distinction first and briefly was the only
    one with it; #126 made it the convention across every `*Update`
    model, which is why TABLE/CLEARABLE are declared here rather than
    left implicit.
    """

    model_config = ConfigDict(extra="forbid")

    # All three columns are nullable on entity_relationships, so all
    # three can be cleared. Asserted against the database's own
    # PRAGMA table_info rather than trusted --
    # api/tests/test_clearable_fields.py derives it, because a
    # hand-written CLEARABLE is a claim and the schema is not.
    TABLE: ClassVar[str] = "entity_relationships"
    CLEARABLE: ClassVar[frozenset[str]] = frozenset(
        ["start_date", "end_date", "attributes"]
    )

    start_date: date | None = None
    end_date: date | None = None
    attributes: dict | None = None

    @field_validator("attributes")
    @classmethod
    def attributes_do_not_store_sensitive_numbers(
        cls, value: dict | None
    ) -> dict | None:
        return refuse_sensitive_numbers_in(value)

    def model_post_init(self, __context) -> None:
        # NOT `_require_at_least_one`, and the reason is that the two
        # rules do not compose. That helper defines "empty" as every
        # field being None — which is exactly what an explicit clear
        # looks like, so `{"end_date": null}` would be refused as an
        # empty body. Here "empty" means no key was SUPPLIED, which is
        # the same intent expressed against model_fields_set.
        #
        # The helper is correct for every other *Update model, because
        # none of them can clear anything. When that changes (filed
        # separately) this logic is what they need, not this override.
        if not self.model_fields_set:
            raise ValueError(
                "at least one of start_date, end_date, attributes is "
                "required — an empty update would report success without "
                "changing anything"
            )

    def cleared(self, field: str) -> bool:
        """Was `field` sent explicitly as null, rather than omitted?

        `model_fields_set` holds the keys the caller actually supplied,
        so it separates "set this to nothing" from "do not touch this".
        Every other *Update model in this repo conflates them and so
        cannot clear anything; that is filed separately rather than
        changed here.
        """
        return field in self.model_fields_set and getattr(self, field) is None


class RelationshipResponse(BaseModel):
    id: str
    from_entity_id: str
    to_entity_id: str
    relationship_type: RelationshipType
    start_date: date | None = None
    end_date: date | None = None
    attributes: dict


# --- Phase 3: financial schemas -------------------------------------------


class CategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StoredText
    parent_id: str | None = None


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    TABLE: ClassVar[str] = "categories"
    CLEARABLE: ClassVar[frozenset[str]] = frozenset(['parent_id'])

    name: StoredText | None = None
    parent_id: str | None = None

    def model_post_init(self, __context) -> None:
        _require_at_least_one(self, "name", "parent_id")
        _refuse_unclearable(self, "name", "parent_id")


class CategoryResponse(BaseModel):
    id: str
    name: str
    parent_id: str | None = None


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _date_only(value: object) -> object:
    """Refuse anything but a bare calendar date on a key field (ticket T-22).

    Pydantic v2 accepts a datetime STRING for a `date` field when the time
    is exactly `00:00:00` -- and only then. It keeps the calendar date as
    written and DISCARDS the offset. Measured on 2.13.5:

        '2026-05-04T00:00:00+01:00'  -> 2026-05-04
        '2026-05-04T00:00:00+14:00'  -> 2026-05-04
        '2026-05-04T00:00:00Z'       -> 2026-05-04
        '2026-05-04T00:00:01+01:00'  -> 422
        '2026-05-04T23:30:00+00:00'  -> 422

    That is a key bug, not a formatting one. `txn_date`, `period_start`
    and `period_end` are part of the account-scoped de-duplication key, and
    `+14:00` midnight is the UTC date 2026-05-03. So two callers naming the
    same instant in different offsets write DIFFERENT keys, and one caller
    naming different instants can write the SAME key -- both with a 200.

    THE UTC FORM IS REFUSED TOO, deliberately. `...T00:00:00Z` looks
    unambiguous to a human, but the offset is discarded either way, so
    accepting it would mean accepting a value whose timezone the system
    then ignores. "Unambiguous to a reader" and "unambiguous to the key"
    are different properties.

    A `date` OBJECT passes: a Python caller that already has a calendar
    date has made the choice this rejects. A `datetime` object does not --
    it is a `date` subclass, so it must be refused explicitly or it would
    arrive as truthy and slip through.
    """
    if isinstance(value, datetime):
        raise ValueError(
            "expected a calendar date (YYYY-MM-DD), not a datetime — the "
            "time and any offset would be discarded, and this field is part "
            "of the de-duplication key"
        )
    if isinstance(value, str) and not _ISO_DATE.fullmatch(value):
        # The rejected value is NOT echoed. `_error_detail` drops
        # pydantic's `input` key for exactly this case -- its docstring
        # names "a mistyped account number or date of birth" -- and
        # echoing the same string into `msg` puts it straight back into
        # the response body, the logs and the MCP transcript. A date
        # field is where a misaimed paste lands, not a place a caller
        # meant to type free text, which is what makes this the likely
        # case rather than the contrived one. Measured: period_start
        # "123-45-6789" came back in full.
        raise ValueError(
            "expected a calendar date (YYYY-MM-DD) — a datetime is "
            "refused even at midnight and even in UTC, because the offset "
            "is discarded and this field is part of the de-duplication key"
        )
    return value


#: A date that is part of a stored key. See `_date_only`.
KeyDate = Annotated[date, BeforeValidator(_date_only)]


def _check_period(period_start: date, period_end: date) -> None:
    """Reject a reversed statement period.

    Nothing downstream would notice: reports aggregate on txn_date, not on
    the statement period, so a reversed period persists silently as
    nonsense metadata. end == start is allowed — a one-day period is
    unusual, not wrong, and rejecting it would be a silent narrowing.
    """
    if period_end < period_start:
        raise ValueError(
            f"period_end ({period_end}) must not precede period_start "
            f"({period_start})"
        )


class StatementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    period_start: KeyDate
    period_end: KeyDate

    def model_post_init(self, __context) -> None:
        _check_period(self.period_start, self.period_end)


class StatementResponse(BaseModel):
    id: str
    account_id: str
    period_start: date
    period_end: date
    imported_at: str


#: Stated once, beside the model that enforces it
#: (StrictInt -- also rejects 2.0 and "250", which a plain int would accept).
#: The tool description interpolates this; a behavioural test
#: exercises it. See api/tests/test_claim_contracts.py.
AMOUNT_CENTS_CONTRACT = "integer cents; a fractional value is rejected, not rounded"


class TransactionInput(BaseModel):
    """One row inside an import batch.

    No `id` and no `statement_id`: both are assigned during import, so
    accepting them here would let a caller attach a transaction to someone
    else's statement. `extra="forbid"` is what enforces that.
    """

    model_config = ConfigDict(extra="forbid")

    txn_date: KeyDate
    description: StoredText
    # Integer cents, never dollars, and strict so a float is rejected
    # rather than coerced. -5210 is $52.10 out. Accepting 52.10 and
    # rounding would put the representation error back exactly where this
    # column exists to remove it, and silently.
    amount_cents: StrictInt


class TransactionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    TABLE: ClassVar[str] = "transactions"
    CLEARABLE: ClassVar[frozenset[str]] = frozenset(['category_id', 'entity_id'])

    category_id: str | None = None
    entity_id: str | None = None

    def model_post_init(self, __context) -> None:
        _require_at_least_one(self, "category_id", "entity_id")
        _refuse_unclearable(self, "category_id", "entity_id")


class ArchiveStatementResponse(BaseModel):
    """What archiving a statement did, and what it will cost to re-import."""

    id: str
    archived: bool
    archived_transactions: int
    #: How many of those rows were hand-edited since import. See
    #: `_set_archived` for exactly what that means.
    hand_edited: int
    #: WHICH rows, not just how many. A count tells the caller something
    #: was lost; the ids let them look at it first and decide, or write
    #: the edits down before re-importing. "Some rows were edited" without
    #: saying which leaves them unable to act on the warning.
    hand_edited_ids: list[str] = []


class ArchiveTransactionResponse(BaseModel):
    id: str
    archived: bool
    hand_edited: int
    hand_edited_ids: list[str] = []


class TransactionResponse(BaseModel):
    id: str
    statement_id: str
    account_id: str
    txn_date: date
    description: str
    amount_cents: int
    category_id: str | None = None
    entity_id: str | None = None


# --- reminders -------------------------------------------------------------


def _validated_rrule(rule: str | None, start: date) -> str | None:
    """Reject an unusable RRULE at write time rather than at read time.

    `GET /due` swallows a malformed rule so one bad row cannot take the
    whole digest down, which means without this check a typo is a
    reminder that silently never fires — and the thing you would use to
    notice is the digest it is missing from. Validating here turns that
    into a 422 at the moment someone can still fix it.

    Parsed WITH a naive `dtstart`, and that makes validation STRICTER,
    not more permissive — the first version of this docstring had it
    backwards (review-1 on #86). `FREQ=DAILY;UNTIL=20261231T000000Z`
    parses fine bare and raises ValueError once a naive dtstart is
    supplied, because dateutil refuses a UTC UNTIL against a
    timezone-naive start.

    The dtstart stays for a better reason: `api/due.py`'s `_occurrences`
    expands with exactly the same naive dtstart, so validating the way
    the digest parses is what makes "accepted by POST /reminders" mean
    "expandable by /due". Validating bare would accept rules the digest
    then silently skips — `/due` swallows an unusable rule so one bad
    row cannot take the whole report down, which is right there and
    wrong here.

    So do not "fix" the strictness by dropping the dtstart. The
    strictness IS the guarantee; loosening it moves the failure from a
    422 someone can act on to a reminder that never fires.
    """
    if rule is None:
        return None
    from dateutil.rrule import rrulestr

    try:
        rrulestr(rule, dtstart=datetime(start.year, start.month, start.day))
    except (ValueError, TypeError) as exc:
        # dateutil's message quotes the caller's own string back --
        # measured: "FREQ=WEEKLY;BYDAY=123-45-6789" produces
        # "invalid 'BYDAY': 123-45-6789". recurrence_rule is free text a
        # caller controls, so the message is not forwarded; the
        # exception is still chained, so the detail is in the traceback
        # for anyone reading the API's own logs, which is where a
        # caller-supplied value may go and a response body is not.
        raise ValueError(
            "recurrence_rule is not a usable RRULE — expected something "
            "like FREQ=MONTHLY;BYMONTHDAY=1 or FREQ=YEARLY. The value is "
            "not repeated here because it is free text you supplied; the "
            "parser's own message is in the API log."
        ) from exc
    return rule


class ReminderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: StoredText = Field(min_length=1)
    notes: StoredText | None = None
    entity_id: str | None = None
    recurrence_rule: str | None = None
    start_date: date
    end_date: date | None = None
    due_time: time | None = None
    location: StoredText | None = None
    notification_offsets_minutes: list[int] = Field(default_factory=lambda: [1440, 60])

    def model_post_init(self, __context) -> None:
        if not 1 <= len(self.notification_offsets_minutes) <= 5 or any(
            value < 0 or value > 40320 for value in self.notification_offsets_minutes
        ):
            raise ValueError("notification_offsets_minutes must contain 1-5 offsets between 0 and 40320 minutes")
        if self.due_time is not None and (self.due_time.tzinfo is not None or self.due_time.microsecond):
            raise ValueError("due_time must be a local wall-clock time with whole seconds, without a UTC offset")
        _validated_rrule(self.recurrence_rule, self.start_date)
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError(
                f"end_date ({self.end_date}) must not precede start_date "
                f"({self.start_date}) — a series that ends before it begins "
                f"never fires, and nothing downstream would report that"
            )
        if self.end_date is not None and self.recurrence_rule is None:
            raise ValueError(
                "end_date is only meaningful with a recurrence_rule: it ends "
                "a series, and a one-off reminder is its start_date"
            )


class ReminderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    TABLE: ClassVar[str] = "reminders"
    CLEARABLE: ClassVar[frozenset[str]] = frozenset(
        {"notes", "entity_id", "recurrence_rule", "end_date", "due_time", "location"}
    )
    title: StoredText | None = Field(default=None, min_length=1)
    notes: StoredText | None = None
    entity_id: str | None = None
    recurrence_rule: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    due_time: time | None = None
    location: StoredText | None = None
    notification_offsets_minutes: list[int] | None = None
    reset_occurrences: bool = False

    def model_post_init(self, __context) -> None:
        fields = tuple(ReminderCreate.model_fields)
        _require_at_least_one(self, *fields)
        _refuse_unclearable(self, *fields)


class ReminderResponse(BaseModel):
    source_kind: str | None = None
    id: str
    title: str
    notes: str | None = None
    entity_id: str | None = None
    recurrence_rule: str | None = None
    start_date: date
    end_date: date | None = None
    due_time: time | None = None
    location: str | None = None
    notification_offsets_minutes: list[int] = Field(default_factory=lambda: [1440, 60])
    created_at: str | None = None


# `due_date` identifies WHICH occurrence, which is why it is required
# rather than defaulting to the next one: a recurring reminder has many,
# and guessing would silently complete the wrong one.
class ReminderInstanceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # KeyDate, not date: (reminder_id, due_date) is UNIQUE and the completion
    # upsert conflict-targets it, so this value decides whether a completion
    # creates a new occurrence or silently overwrites an existing one.
    due_date: KeyDate


class ReminderSnooze(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Same key column as ReminderInstanceCreate.due_date -- it selects which
    # occurrence gets snoozed. snoozed_to is a value column written via
    # `excluded.snoozed_to`; it never becomes the conflict target.
    due_date: KeyDate
    snoozed_to: date

    def model_post_init(self, __context) -> None:
        if self.snoozed_to <= self.due_date:
            raise ValueError(
                f"snoozed_to ({self.snoozed_to}) must be after due_date "
                f"({self.due_date}) — snoozing to the same day or earlier "
                f"would leave the item due and look like it worked"
            )


class ReminderInstanceResponse(BaseModel):
    id: str
    reminder_id: str
    due_date: date
    status: str
    completed_at: str | None = None
    snoozed_to: date | None = None


class MerchantRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    #: Who is making this change. Content-required, not merely present —
    #: see TransitionCreate, from which this validation is copied rather
    #: than reinvented: an unattributed row is barely better than a
    #: missing one, since "who changed this" is half of what a trail is
    #: for, and requiring the field alone lets actor="" satisfy the rule
    #: and defeat its purpose in the same call.
    actor: StoredText = Field(min_length=1)
    pattern: StoredText
    category_id: str | None = None
    entity_id: str | None = None

    def model_post_init(self, __context) -> None:
        if self.category_id is None and self.entity_id is None:
            raise ValueError(
                "at least one of category_id or entity_id is required — "
                "a rule that sets neither classifies nothing"
            )


class MerchantRuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    TABLE: ClassVar[str] = "merchant_rules"
    CLEARABLE: ClassVar[frozenset[str]] = frozenset(["category_id", "entity_id"])

    #: See MerchantRuleCreate.
    actor: StoredText = Field(min_length=1)
    pattern: StoredText | None = None
    category_id: str | None = None
    entity_id: str | None = None

    def model_post_init(self, __context) -> None:
        # An empty PATCH was a 200 that changed nothing while reporting a
        # rule back, which reads exactly like a successful edit. Same
        # silent-no-op shape as update_entity's dropped fields: the caller
        # believes the correction landed and the rule still misclassifies
        # every future import.
        #
        # "At least one field", not "at least one of category/entity" --
        # a pattern-only PATCH is legitimate at this layer. The restriction
        # against pattern changes is a TOOL-surface decision (a todo,
        # a model must not fight longest-pattern-wins by hand), so it
        # belongs in the tool signature, not in the API model.
        # Was an inline all-None copy of _require_at_least_one. Converted
        # with the rest (ticket T-17): "every field is None" is exactly what an
        # explicit clear looks like, so the old form refused
        # {"category_id": null} as an empty body.
        _require_at_least_one(self, "pattern", "category_id", "entity_id")
        _refuse_unclearable(self, "pattern", "category_id", "entity_id")


class ApplyAsRuleRequest(BaseModel):
    """`apply_as_rule` had no body at all; it needs one now for `actor`.

    A body rather than a query parameter because it is a POST, and POST
    carries actor in the body everywhere else here. DELETE is the sole
    exception, for the reason given on delete_merchant_rule.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor: StoredText = Field(min_length=1)


class MerchantRuleChangeResponse(BaseModel):
    id: str
    rule_id: str
    changed_at: str
    actor: str
    field: str | None = None
    old_value: str | None = None
    new_value: str | None = None


class MerchantRuleResponse(BaseModel):
    id: str
    pattern: str
    category_id: str | None = None
    entity_id: str | None = None


class ReapplyRulesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Default TRUE. A tool that rewrites rows across the whole history
    # should make you ask for the write, not remember to withhold it.
    dry_run: bool = True
    statement_id: str | None = None
    entity_id: str | None = None


class ReapplyRulesResponse(BaseModel):
    dry_run: bool
    examined: int
    changed: int
    #: category_id -> cents that would move into it. Lets a caller see the
    #: size of the change before authorising it, not just the row count.
    cents_by_category: dict[str, int]
    #: Every id touched, so a wrong reapply can be undone by hand. There is
    #: no undo; this list is it.
    changed_ids: list[str]


class ImportStatementRequest(BaseModel):
    """Canonical extracted statement shape; base for source-tracked requests.

    Retained for extraction/reference validation, not a legacy HTTP route.
    """
    model_config = ConfigDict(extra="forbid")

    account_id: str
    period_start: KeyDate
    period_end: KeyDate
    transactions: list[TransactionInput]

    def model_post_init(self, __context) -> None:
        # Same check as StatementCreate: this request creates a statement
        # too, so leaving it off here would just move the hole.
        _check_period(self.period_start, self.period_end)


#: A board's ticket-key prefix: an upper-case letter, then one to five
#: upper-case letters or digits (ticket T-19). api/jyra.py's
#: derive_key_prefix only ever produces a match.
KEY_PREFIX_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,5}$")


def _normalize_key_prefix(value: str | None) -> str | None:
    """Upper-cased, then held to KEY_PREFIX_PATTERN. Keys match in any case,
    so a prefix typed in lower case means the same thing and is stored the
    one way lookups compare against."""
    if value is None:
        return None
    normalized = value.strip().upper()
    if not KEY_PREFIX_PATTERN.match(normalized):
        raise ValueError(
            "key_prefix must be a letter followed by one to five letters or "
            "digits, such as KA or KCX"
        )
    return normalized


class BoardCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str
    title: StoredText
    #: Optional. Absent, one is derived from the entity's name and made
    #: unique (api/jyra.py derive_key_prefix).
    key_prefix: str | None = None

    _prefix = field_validator("key_prefix")(_normalize_key_prefix)


class BoardUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    TABLE: ClassVar[str] = "boards"
    CLEARABLE: ClassVar[frozenset[str]] = frozenset([])

    title: StoredText | None = None
    key_prefix: str | None = None

    _prefix = field_validator("key_prefix")(_normalize_key_prefix)

    def model_post_init(self, __context) -> None:
        _require_at_least_one(self, "title", "key_prefix")
        _refuse_unclearable(self, "title", "key_prefix")


class BoardResponse(BaseModel):
    id: str
    entity_id: str
    title: str
    key_prefix: str
    created_at: str


TicketType = Literal["solution", "epic", "story", "bug", "task"]
TicketStatus = Literal[
    "backlog",
    "in_progress",
    "agent_ready",
    "agent_coding",
    "review",
    "blocked",
    "done",
]

# Solutions and epics are planning containers — an agent never picks one
# up, so the three agent-facing statuses don't apply. `blocked` does apply
# to every type: an epic can be stuck on an outside dependency too.
_PLANNING_STATUSES = frozenset({"backlog", "in_progress", "blocked", "done"})
_DELIVERY_STATUSES = frozenset(
    {
        "backlog",
        "in_progress",
        "agent_ready",
        "agent_coding",
        "review",
        "blocked",
        "done",
    }
)

STATUSES_BY_TYPE = {
    "solution": _PLANNING_STATUSES,
    "epic": _PLANNING_STATUSES,
    "story": _DELIVERY_STATUSES,
    "bug": _DELIVERY_STATUSES,
    "task": _DELIVERY_STATUSES,
}

# A parent must strictly outrank its child. This blocks nonsense (a bug
# parenting an epic) without imposing Jira's rigid pairings — a task can
# hang directly off a solution when that's the honest structure.
#
# Strictness is also what makes cycles impossible: rank strictly decreases
# from parent to child, so no chain can return to where it started and no
# cycle check is needed anywhere in the ticket routes.
TYPE_RANK = {"solution": 3, "epic": 2, "story": 1, "bug": 1, "task": 1}


def validate_ticket_status(
    ticket_type: str, status: str, field: str = "status"
) -> None:
    """Raise RequestValidationError (422) if `status` isn't legal for
    `ticket_type`.

    A 422 rather than InvalidReferenceError's 400: this is a value outside its
    permitted range, not a reference to something that doesn't exist. It is
    the same per-type validation validate_entity_attributes performs, and
    since Phase 2's RequestValidationError handler wraps 422s in the standard
    {"error": {...}} envelope, it is exactly as consistent as the api/errors.py
    types — a 422 here is no longer a different response shape.

    `field` names the request field being validated so the error's `loc`
    points at it: routes that create a ticket send `status`, while the
    transition endpoint sends `to_status`.

    `ticket_type` is assumed to be a TicketType value — an unknown one raises
    KeyError on STATUSES_BY_TYPE rather than validating. Same accepted
    precondition as ATTRIBUTE_MODELS[entity_type] in
    validate_entity_attributes: callers only ever pass a value that already
    passed TicketType's Literal validation or was read back from a stored row.
    """
    if status not in STATUSES_BY_TYPE[ticket_type]:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("body", field),
                    "msg": (
                        f"Status {status!r} is not valid for "
                        f"{'an' if ticket_type[0] in 'aeiou' else 'a'} "
                        f"{ticket_type} ticket"
                    ),
                    "input": status,
                }
            ]
        )


def validate_parent_rank(parent_type: str, child_type: str) -> None:
    """Raise InvalidReferenceError unless `parent_type` strictly outranks
    `child_type`.

    A 400 invalid_reference, not the 422 validate_ticket_status uses, and the
    difference is the rule rather than an inconsistency: 422 is for a value
    wrong on its own terms (a status outside the enum, independent of any
    other row), while invalid_reference is for a value only wrong *because of*
    another row's state — the parent exists and is a legal ticket, and it is
    the combination that is not allowed. Settled deliberately; please do not
    reopen it as a bug.

    `parent_type` and `child_type` are assumed to be TicketType values, so a
    typo raises KeyError on TYPE_RANK rather than validating. Same accepted
    precondition as ATTRIBUTE_MODELS[entity_type] in
    validate_entity_attributes: callers only ever pass a value that already
    passed TicketType's Literal validation or was read back from a stored row.
    """
    if TYPE_RANK[parent_type] <= TYPE_RANK[child_type]:
        raise InvalidReferenceError(
            f"A {parent_type} cannot be the parent of a {child_type}"
        )


class TicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    board_id: str
    type: TicketType
    title: StoredText
    description: StoredText | None = None
    parent_id: str | None = None
    actor: StoredText


#: Stated once, beside the model that enforces it
#: (no status field, and extra="forbid" rejects one rather than ignoring it).
#: The tool description interpolates this; a behavioural test
#: exercises it. See api/tests/test_claim_contracts.py.
TICKET_UPDATE_CONTRACT = "cannot change status"


class TicketUpdate(BaseModel):
    """Deliberately has no `status` field. extra="forbid" turns an attempt to
    patch status into a 422 rather than a silent no-op — status moves only
    through POST /tickets/{id}/transition."""

    model_config = ConfigDict(extra="forbid")

    TABLE: ClassVar[str] = "tickets"
    CLEARABLE: ClassVar[frozenset[str]] = frozenset(
        ['description', 'parent_id', 'position']
    )

    title: StoredText | None = None
    description: StoredText | None = None
    parent_id: str | None = None
    position: int | None = None

    def model_post_init(self, __context) -> None:
        _require_at_least_one(self, "title", "description", "parent_id", "position")
        _refuse_unclearable(self, "title", "description", "parent_id", "position")


class TicketResponse(BaseModel):
    id: str
    #: The human-readable alias, e.g. KA-12: board key_prefix and number.
    key: str | None = None
    number: int | None = None
    board_id: str
    parent_id: str | None = None
    type: TicketType
    title: str
    description: str | None = None
    status: TicketStatus
    position: int | None = None
    claimed_by: str | None = None
    claimed_at: str | None = None
    created_at: str
    updated_at: str | None = None


#: Stated once, beside the model that enforces it
#: (Field(min_length=1) with str_strip_whitespace, so "" and "   " are both 422).
#: The tool description interpolates this; a behavioural test
#: exercises it. See api/tests/test_claim_contracts.py.
TRANSITION_NOTE_CONTRACT = "note and actor are required and must not be blank"


class TransitionCreate(BaseModel):
    """`note` is required here, which is the whole point — the audit trail is
    only trustworthy if it cannot be skipped. It stays nullable in SQL purely
    for the creation row, which has no "why".

    Required in CONTENT, not merely in presence. Requiring the field alone
    left note="" returning 200 and writing a history row that explained
    nothing, so a caller unwilling to explain itself could satisfy the rule
    and defeat its purpose in the same call — the aspirational version of
    the guarantee the design claims. Same for actor: an unattributed row is
    barely better than a missing one, since "who moved this" is half of what
    the trail is for.

    min_length applies after the trim, so whitespace-only fails and a
    surviving value is stored exactly as validated rather than with padding
    a later reader has to squint past.

    Deliberately no minimum beyond one character. "Say something" is a rule
    an API can hold; "say enough" would be it judging what counts as a good
    reason, which it cannot do and would only teach callers to pad.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_transition_id: UUID | None = None
    to_status: TicketStatus
    actor: StoredText = Field(min_length=1)
    note: StoredText = Field(min_length=1)


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: StoredText
    board_id: str | None = None


class AttachmentResponse(BaseModel):
    """No `file_path`, deliberately. Content is reached by id, through
    `GET /attachments/{id}/content`, which looks the path up server-side.

    It used to carry the absolute path under data/jyra/. review-1 flagged
    that in 2026 as setting up a predictable bypass -- a future consumer
    would use the path the API handed back rather than the id-based
    storage scheme -- and the consumer duly arrived, as instructions in
    two tool descriptions telling a model to read the path off disk.
    """

    id: str
    ticket_id: str
    #: A DISPLAY LABEL, never a path component. `_safe_filename` reduces
    #: it to a basename and strips leading dots, but it decodes nothing,
    #: so a percent-encoded traversal survives as a label:
    #: `..%2F..%2Fetc%2Fpasswd` is stored as `%2F..%2Fetc%2Fpasswd`,
    #: which still decodes to `/../etc/passwd`. Decoding it before use
    #: reconstructs the traversal the storage layer declined to perform.
    #: The file on disk is named `{attachment_id}{suffix}` and never from
    #: this value.
    filename: str
    content_hash: str
    #: Size of the stored bytes and the media type, both derived at
    #: upload and stored on the row -- never recomputed per list, which
    #: would turn one query into N and create a second derivation that
    #: can disagree with the first. NULL on rows written before these
    #: columns existed, and reported as null: never 0, and never a type
    #: guessed from a client-derived suffix, because a plausible wrong
    #: value is worse than an absent one.
    size_bytes: int | None = None
    content_type: str | None = None
    uploaded_at: str


class TicketRef(BaseModel):
    """Enough of a ticket to show it in a list and fetch it, and no more.

    Children are embedded as refs rather than whole rows so a large epic's
    detail response stays bounded — description is the field that would make
    it unbounded, so it is deliberately absent.
    """

    id: str
    key: str | None = None
    type: TicketType
    title: str
    status: TicketStatus


class LinkedArtifactRef(BaseModel):
    """An artifact linked to a ticket, as get_ticket embeds it."""

    link_id: str
    artifact_id: str
    kind: str
    title: str | None
    current_revision: int
    viewer_path: str


class TicketDetailResponse(TicketResponse):
    """What GET /tickets/{id} returns: the row plus the context the design
    promises — "includes attachments and parent/children refs".

    Separate from TicketResponse because only the single-ticket read embeds.
    A listing that expanded every ticket's attachments would turn one query
    into N, which is the cost the board view deliberately avoids; the create
    and update routes keep returning the plain row for the same reason.

    parent_id is inherited and is the parent ref.
    """

    attachments: list[AttachmentResponse] = []
    #: Total attachments, which may exceed len(attachments) when the embed
    #: is capped. Without it a truncated list looks like the whole set.
    attachment_count: int = 0
    children: list[TicketRef] = []
    #: Linked Canvas artifacts, oldest link first, capped at
    #: api.artifact_links.TICKET_ARTIFACT_CAP; artifact_count is the true total.
    artifacts: list[LinkedArtifactRef] = []
    artifact_count: int = 0




#: The doc_type vocabulary, from db/schema.sql's column comment. Two places
#: describing one set, so api/tests/test_documents_crud.py pins them equal.
DocType = Literal[
    "statement",
    "receipt",
    "tax",
    "insurance_policy",
    "lease",
    "title_deed",
    "warranty",
    "correspondence",
    "other",
]

#: The same nine values as a tuple, DERIVED from the Literal above rather
#: than retyped, so the reader's filter and the writer's field cannot
#: drift apart -- which is the defect this exists for (ticket T-11).
DOC_TYPES: tuple[str, ...] = get_args(DocType)


class DocumentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str
    title: StoredText
    doc_type: DocType | None = None
    entity_id: str | None = None


class DocumentResponse(BaseModel):
    id: str
    entity_id: str | None = None
    title: str
    doc_type: str | None = None
    content_hash: str
    imported_at: str
