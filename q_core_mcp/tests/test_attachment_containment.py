"""attach_file must not carry an unredacted document into the store.

The measured defect: `attach_file` validated `exists()` and `is_file()`
and nothing else — no containment, no cap, no redaction — while
`api/documents.py` carries eight scrub/containment guards. So a
model-supplied path could read a real statement out of `intake/` and post
it into the attachment store, bypassing the pipeline #68 and #74 exist to
enforce and breaching CLAUDE.md's standing invariant.

THE CONTENT CHECK IS THE PRIMARY CONTROL, not defence in depth, and the
reason is measured rather than stylistic: no directory on this machine is
safe by location. `data/documents/` holds the ORIGINAL bytes of every
registered document, because register_document moves the file rather than
a scrubbed copy. Containment is the outer bound — it stops arbitrary
reads; it cannot certify that what was read is clean.
"""

from pathlib import Path

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from api.documents import MAX_ATTACHMENT_BYTES
from api.tests.pdf_fixture import make_imageless_pdf, make_pdf
from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server

FAKE_CARD = "4532015112830366"


def _server(test_settings):
    return build_server(
        QCoreClient(
            test_settings,
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"id": "a1"})
            ),
        )
    )


def _attach(call_tool, test_settings, path):
    return call_tool(
        _server(test_settings), "attach_file",
        {"ticket_id": "t1", "path": str(path)},
    )


# --- containment: the outer bound -----------------------------------------


#: A fragment of the reason each hole must give. Pinned because a refusal
#: that does not say why reads as arbitrary, and the reader most likely to
#: meet it is a model deciding whether to try something else.
HOLE_REASONS = {
    "intake_dir": "real bank statements",
    "inbox_dir": "unprocessed documents",
    "documents_dir": "ORIGINAL bytes",
    "logs_dir": "request detail",
    "jyra_dir": "other tickets",
}


@pytest.mark.parametrize("hole", sorted(HOLE_REASONS))
@pytest.mark.parametrize("root", ["configured", "/"])
def test_the_sensitive_directories_are_refused(
    test_settings, call_tool, tmp_path, hole, root
):
    """intake/ holds statements; inbox/ holds unprocessed documents; the
    documents store holds ORIGINALS, not scrubbed copies; logs carry
    request detail; the attachment store holds other tickets' files.

    `logs_dir` and `jyra_dir` are new rows, and `logs_dir` is why: it was
    pinned only as a STRING the description had to contain, so review-1
    neutered the hole with the description untouched and the suite stayed
    green. Writing this test then found the entry was wrong as well as
    untested — it named `REPO_ROOT / "logs"`, a directory that does not
    exist, while the real logs live at `settings.logs_dir` under data/ and
    were covered only incidentally by the data hole. `logs_dir` is
    configurable, so moving it outside data/ would have exposed them.

    Crossed with root ∈ {configured, "/"} because a hole that holds only
    while the roots are narrow is not a hole.
    """
    settings = (
        test_settings
        if root == "configured"
        else test_settings.model_copy(update={"attachment_roots": ("/",)})
    )
    directory = Path(getattr(settings, hole))
    directory.mkdir(parents=True, exist_ok=True)
    victim = directory / "statement.pdf"
    victim.write_bytes(make_pdf(["ACCOUNT 12345678 BALANCE 1,204.55"]))

    with pytest.raises(ToolError) as excinfo:
        _attach(call_tool, settings, victim)

    message = str(excinfo.value)
    assert "will not read" in message
    assert "register_document" in message, "a refusal should name the right path"
    # And the REASON, per hole. Emptying a reason string left every other
    # assertion here green -- the refusal still fired, it just stopped
    # explaining itself. That is the same "neutered entry" shape review-1
    # got past, one level in: the guard works and the message it produces
    # is worthless, which nothing noticed.
    assert HOLE_REASONS[hole] in message, (
        f"the refusal no longer says WHY {hole} is refused"
    )


def test_the_database_itself_is_refused(test_settings, call_tool):
    database = Path(test_settings.db_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"SQLite format 3\x00")

    with pytest.raises(ToolError) as excinfo:
        _attach(call_tool, test_settings, database)

    assert "database" in str(excinfo.value).lower()


def test_a_path_outside_every_root_is_refused(test_settings, call_tool, tmp_path):
    outside = tmp_path.parent / "outside-the-roots.txt"
    outside.write_text("hello")

    with pytest.raises(ToolError) as excinfo:
        _attach(call_tool, test_settings, outside)

    assert "only read files under" in str(excinfo.value)


def test_a_symlink_cannot_smuggle_a_path_out_of_a_hole(
    test_settings, call_tool, tmp_path
):
    """Resolved BEFORE the check, or containment is decorative: a link
    inside an allowed root pointing at intake/ would otherwise pass."""
    intake = Path(test_settings.intake_dir)
    intake.mkdir(parents=True, exist_ok=True)
    victim = intake / "statement.pdf"
    victim.write_bytes(make_pdf(["ACCOUNT 12345678"]))
    link = tmp_path / "innocent.pdf"
    link.symlink_to(victim)

    with pytest.raises(ToolError) as excinfo:
        _attach(call_tool, test_settings, link)

    assert "will not read" in str(excinfo.value)


# --- the content check: the primary control -------------------------------


def test_a_file_carrying_an_account_number_is_refused_from_an_allowed_root(
    test_settings, call_tool, tmp_path
):
    """The test that proves the content check does the work.

    This file is in an ALLOWED root — containment passes it. Only the
    content check stands between it and the store, which is the whole
    argument for making that check primary.
    """
    from api.config import get_settings
    from api.main import app

    receipt = tmp_path / "receipt.txt"
    receipt.write_text(f"AUTOPARTS DEPOT\nCard {FAKE_CARD}\nTOTAL 84.99")

    # The REAL app, not a mock: the content check is API-side by design,
    # so a mocked transport would pass this and the test would report the
    # primary control working when nothing had run it.
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = build_server(
            QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
        )
        # A REAL ticket: the endpoint validates the ticket before it reads
        # the payload, so a placeholder id would fail on the lookup and the
        # test would report the content check working without running it.
        entity = call_tool(
            server, "create_entity", {"entity_type": "project", "name": "P"}
        )
        board = call_tool(
            server, "create_board", {"entity_id": entity["id"], "title": "B"}
        )
        created = call_tool(
            server, "create_ticket",
            {"board_id": board["id"], "title": "T", "ticket_type": "task",
             "actor": "impl-4"},
        )
        with pytest.raises(ToolError) as excinfo:
            call_tool(
                server, "attach_file",
                {"ticket_id": created["id"], "path": str(receipt)},
            )
    finally:
        app.dependency_overrides.clear()

    message = str(excinfo.value)
    assert "verbatim" in message or "account" in message
    assert FAKE_CARD not in message, "a refusal must not echo what it found"


def test_a_clean_file_from_an_allowed_root_is_accepted(
    test_settings, call_tool, tmp_path
):
    """The guard must not fail closed on ordinary use."""
    note = tmp_path / "note.txt"
    note.write_text("ordered the part, arrives Tuesday, 84.99 total")

    assert _attach(call_tool, test_settings, note)["id"] == "a1"


def test_a_scanned_pdf_is_treated_as_a_binary_not_as_clean(
    test_settings, call_tool, tmp_path
):
    """The case that would otherwise wave through the worst file here.

    A scanned statement is a PDF the extractor cannot read. "No text
    found" must mean UNKNOWN, not "no account numbers present" — treating
    it as clean would pass the single most sensitive document on this
    machine. It is accepted only because containment already restricted
    the root, and the description says so rather than implying it was
    checked.
    """
    from api.documents import attachment_text_or_none

    scanned = tmp_path / "scan.pdf"
    payload = make_imageless_pdf()
    scanned.write_bytes(payload)

    # Asserted at the UNIT, because at the boundary the distinction is
    # invisible: assert_no_account_numbers("") passes trivially, so
    # "unknown" and "clean" both end in acceptance. Mutating the extractor
    # to return "" instead of None left every boundary test green -- data
    # that cannot tell the two answers apart, for the third time today.
    #
    # It is not a distinction without a difference, though. It is the
    # difference that protects the NEXT change: the day anyone relaxes
    # containment for text that passed the check, returning "" here would
    # let a scanned statement through from anywhere, because it would
    # have been recorded as verified rather than unread.
    assert attachment_text_or_none(payload, "scan.pdf") is None, (
        "a PDF that yields no text must read as UNKNOWN, never as clean"
    )

    assert _attach(call_tool, test_settings, scanned)["id"] == "a1"


# --- the cap, at both ends independently ----------------------------------


def test_one_byte_over_the_cap_is_refused_by_the_tool(
    test_settings, call_tool, tmp_path
):
    big = tmp_path / "big.bin"
    big.write_bytes(b"\x00" * (MAX_ATTACHMENT_BYTES + 1))

    with pytest.raises(ToolError) as excinfo:
        _attach(call_tool, test_settings, big)

    message = str(excinfo.value)
    assert str(MAX_ATTACHMENT_BYTES) in message, "the refusal must name the limit"
    assert "before reading" in message


def test_the_attachment_contract_is_pinned_exactly():
    """Exact, per #107: a lower bound misses an ADDED clause, and an
    addition is the edit that reaches the description without passing
    through anything."""
    from api.documents import ATTACHMENT_CONTRACT

    assert ATTACHMENT_CONTRACT == (
        "attachments are stored VERBATIM and never redacted; a file whose "
        "text carries an account number is refused rather than scrubbed. What "
        "counts as text is decided by CONTENT, not by suffix -- anything that "
        "decodes as UTF-8 is checked whatever it is named -- and only a file "
        "that genuinely cannot be read, a binary or a scanned PDF, is treated "
        "as unknown rather than clean, so it is allowed only because its "
        "directory is"
    )


def test_the_description_tells_a_model_all_of_it(test_settings, tools_by_name):
    """A model has no way to know the attachment path differs from the
    documents path unless the description says so."""
    from api.documents import ATTACHMENT_CONTRACT

    description = tools_by_name(_server(test_settings))["attach_file"].description

    assert ATTACHMENT_CONTRACT in description
    lowered = description.lower()
    assert "register_document" in description, "the refusal needs a right answer"
    for word in ("intake", "inbox", "documents", "backups", "secrets", "redaction profile", "/proc", "environment files"):
        assert word in lowered, f"the description must name the {word} refusal"


# --- the default, and the holes that survive any root ---------------------


def test_the_env_file_is_refused_even_with_the_root_set_to_everything(
    test_settings, call_tool, tmp_path, monkeypatch
):
    """The defect review-1 found, and the reason holes are not configurable.

    The first version defaulted `attachment_roots` to the working tree with
    holes for the financial surface only — so `.env` was readable, and an
    attachment can be read back by anyone who can read the ticket. That
    hands over Q_CORE_API_TOKEN.

    It was a deny-list that enumerated SOME of the dangerous things, which
    is the failure #70 removed — and I had written that warning into this
    very file before shipping it one step short.

    Root set to "/" deliberately: if the hole only holds while the roots
    are narrow, it is not a hole, it is a side effect.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("Q_CORE_API_TOKEN=sekrit\n")
    from api.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", str(env_file))
    settings = test_settings.model_copy(update={"attachment_roots": ("/",)})

    with pytest.raises(ToolError) as excinfo:
        _attach(call_tool, settings, env_file)

    message = str(excinfo.value)
    assert "environment file" in message
    assert "sekrit" not in message, "a refusal must not echo the secret"


def test_the_rest_of_data_is_refused_even_with_the_root_set_to_everything(
    test_settings, call_tool
):
    """data/ gains directories over time — jyra/ did, documents/ did — so
    the rule is "all of data/ except the configured roots inside it",
    never a list of the ones to refuse."""
    jyra = Path(test_settings.jyra_dir)
    jyra.mkdir(parents=True, exist_ok=True)
    stored = jyra / "someone-elses-attachment.png"
    stored.write_bytes(b"\x89PNG")
    settings = test_settings.model_copy(update={"attachment_roots": ("/",)})

    with pytest.raises(ToolError) as excinfo:
        _attach(call_tool, settings, stored)

    # Refused by the NAMED jyra_dir hole, which is stronger than the
    # data-scope rule it used to rely on: the attachment store is refused
    # wherever it is configured, not only while it happens to sit in data/.
    assert "will not read" in str(excinfo.value)
    assert "attachment store" in str(excinfo.value)


def test_the_default_root_is_one_directory_that_holds_nothing_else():
    """A positive allow-list, not the working tree with holes cut in it."""
    from api.config import REPO_ROOT, Settings

    roots = Settings(_env_file=None, api_token="x").attachment_roots

    assert roots == (str(REPO_ROOT / "data" / "attachments-inbox"),)


def test_a_clean_file_in_the_default_root_is_accepted(
    test_settings, call_tool, tmp_path
):
    """The default must be usable, or the first thing anyone does is widen
    it — which is the setting the default exists to keep them out of."""
    root = tmp_path / "attachments-inbox"
    root.mkdir()
    note = root / "screenshot-notes.txt"
    note.write_text("the button is misaligned on the settings page")
    settings = test_settings.model_copy(update={"attachment_roots": (str(root),)})

    assert _attach(call_tool, settings, note)["id"] == "a1"


def test_the_description_states_the_default_and_that_widening_is_deliberate(
    test_settings, tools_by_name
):
    description = tools_by_name(_server(test_settings))["attach_file"].description
    lowered = description.lower()

    assert "data/attachments-inbox" in lowered
    assert "deliberate" in lowered
    for refused in (".git", "logs/", "environment file"):
        assert refused in lowered, f"the description must name the {refused} refusal"


@pytest.mark.parametrize("root", ["/", "data"])
def test_the_data_hole_holds_for_a_narrow_root_as_well_as_the_widest(
    test_settings, call_tool, root, monkeypatch, tmp_path
):
    """`is_relative_to` is REFLEXIVE, and that inverted the allow-list.

    `attachment_roots = "data/"` made data_dir.is_relative_to(data_dir)
    true, so the root carved a hole covering ALL of data/ — a NARROWER
    root granting more than `"/"` did. Both values are tested because the
    first version passed `"/"` and failed `"data"`, and only one of them
    was in the test.
    """
    from api.config import REPO_ROOT

    data_dir = REPO_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    victim = data_dir / "not-an-attachment.txt"
    victim.write_text("something that lives in data/")

    chosen = "/" if root == "/" else str(data_dir)
    settings = test_settings.model_copy(update={"attachment_roots": (chosen,)})

    with pytest.raises(ToolError) as excinfo:
        _attach(call_tool, settings, victim)

    assert "except the configured attachment roots" in str(excinfo.value)
    victim.unlink()


def test_the_data_hole_does_not_move_when_the_database_does(
    test_settings, call_tool, tmp_path
):
    """The hole is data/ BY IDENTITY, not the database's parent.

    With `db_path` elsewhere the old derivation left the rest of data/
    readable; with the database at ~/q-core.db it made the whole home
    directory a hole and refused a Desktop screenshot. The hole must not
    move when an unrelated setting does.
    """
    from api.config import REPO_ROOT

    data_dir = REPO_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    victim = data_dir / "still-refused.txt"
    victim.write_text("in data/, whatever db_path says")

    elsewhere = tmp_path / "somewhere-else" / "q-core.db"
    elsewhere.parent.mkdir(parents=True, exist_ok=True)
    settings = test_settings.model_copy(
        update={"attachment_roots": ("/",), "db_path": str(elsewhere)}
    )

    with pytest.raises(ToolError) as excinfo:
        _attach(call_tool, settings, victim)

    assert "except the configured attachment roots" in str(excinfo.value)
    victim.unlink()


def test_a_missing_configured_root_fails_the_attach_not_the_database(
    test_settings, call_tool, tmp_path
):
    """A typo'd setting must break attaching, not reading a transaction.

    init_db used to mkdir every configured root, per request through
    get_connection, so a bad value 500'd every database request with an
    error naming neither attachments nor the setting.
    """
    settings = test_settings.model_copy(
        update={"attachment_roots": (str(tmp_path / "typo-does-not-exist"),)}
    )
    victim = tmp_path / "note.txt"
    victim.write_text("fine")

    with pytest.raises(ToolError) as excinfo:
        _attach(call_tool, settings, victim)

    assert "does not exist" in str(excinfo.value)
    assert "attachment_roots" in str(excinfo.value), "name the setting at fault"

    # And init_db never touches a configured root, so a bad one cannot
    # reach the connection dependency at all. Asserted on the source
    # rather than by driving a request: the failure being ruled out is
    # "mkdir on a user-supplied path inside get_connection", and that is a
    # property of the code, not of one request that happened to succeed.
    from api.config import REPO_ROOT

    startup = (REPO_ROOT / "api" / "db.py").read_text()
    assert "for root in settings.attachment_roots" not in startup


@pytest.mark.parametrize("root", ["configured", "/"])
def test_the_git_directory_is_refused(test_settings, call_tool, root):
    """`.git` holds remotes, credential helpers and every past version.

    MEASURED CAVEAT, which is why this is not in the parametrized test
    above. In a git WORKTREE, `.git` is a FILE, not a directory:

        $ ls -l .git
        -rw-r--r--  59  .git
        $ cat .git
        gitdir: /path/to/q-core/.git/worktrees/attach

    So `REPO_ROOT / ".git"` is the pointer, and the real object store
    lives in the MAIN checkout, outside REPO_ROOT entirely. A naive case
    here passes for the wrong reason: it proves the pointer is refused,
    which is nearly worthless, while the objects it names remain readable
    with a wide enough root.

    Both facts are asserted separately below rather than conflated. The
    limit is stated rather than fixed because fixing it means resolving
    the gitdir pointer — a behaviour change, and this PR is tests.

    In the main checkout, where q-core actually runs, `.git` IS a
    directory and the hole covers the object store. The gap is
    development-time only, which is why it is a note and not a ticket.
    """
    from api.config import REPO_ROOT

    git_path = REPO_ROOT / ".git"
    assert git_path.exists(), "no .git to test against"

    settings = (
        test_settings
        if root == "configured"
        else test_settings.model_copy(update={"attachment_roots": ("/",)})
    )

    with pytest.raises(ToolError) as excinfo:
        _attach(call_tool, settings, git_path)

    message = str(excinfo.value)
    if root == "/":
        # Only this row exercises the HOLE. Asserted specifically, because
        # the other row is refused by containment and a looser assertion
        # would let both pass while only one tested anything.
        assert "will not read" in message
    else:
        # Under the default root, .git is outside it, so containment
        # refuses first and the hole is never reached. Recorded rather
        # than skipped: "refused" here is a weaker fact than it looks.
        assert "only read files under" in message


def test_the_git_hole_covers_the_pointer_not_the_objects_in_a_worktree():
    """States the limit as an assertion, so it cannot rot into folklore.

    If `.git` is a directory (a normal checkout), the hole covers the
    object store and there is nothing to warn about. If it is a file (a
    worktree), the objects live elsewhere and this hole does not reach
    them — recorded here so the next person reads a measured fact rather
    than discovering it.
    """
    from api.config import REPO_ROOT

    git_path = REPO_ROOT / ".git"

    if git_path.is_dir():
        return  # normal checkout: the hole covers everything under it

    pointer = git_path.read_text()
    assert pointer.startswith("gitdir:"), pointer
    real_store = Path(pointer.split(":", 1)[1].strip())
    assert not real_store.is_relative_to(REPO_ROOT), (
        "in a worktree the object store is outside REPO_ROOT, so the "
        ".git hole covers the pointer only — stated, not fixed, because "
        "resolving the pointer is a behaviour change"
    )


# --- an installed layout: data outside REPO_ROOT (review of q-core #10) ------


@pytest.mark.parametrize("root", ["data", "/"])
@pytest.mark.parametrize("victim", [
    "secrets/google-refresh-token",
    "privacy/redaction.json",
    "backups/q-core-2026-09-25T02-30-00Z.db",
    "forecast/plan.json",
    "some-future-directory/file.txt",
])
def test_the_data_hole_follows_the_configured_data_dir(test_settings, call_tool, tmp_path, root, victim):
    """Installed from the Nix store, REPO_ROOT/data is not where the data is.

    The service sets data_dir (and the paths inside it); every one of these
    was attachable once attachment_roots was widened, because the data hole
    was computed from REPO_ROOT. Invented contents, never real ones.
    """
    data = tmp_path / "srv" / "data"
    path = data / victim
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("invented, not a real secret\n")
    settings = test_settings.model_copy(update={
        "data_dir": str(data),
        "secrets_dir": str(data / "secrets"),
        "db_path": str(data / "q-core.db"),
        "privacy_profile_path": str(data / "privacy" / "redaction.json"),
        "attachment_roots": ("/",) if root == "/" else (str(data),),
    })
    with pytest.raises(ToolError) as excinfo:
        _attach(call_tool, settings, path)
    assert "will not read" in str(excinfo.value)
    assert "invented" not in str(excinfo.value)


def test_the_service_environment_file_is_refused_with_the_root_set_to_everything(
    test_settings, call_tool, tmp_path
):
    """systemd's EnvironmentFile is not .env; the service names it."""
    env_file = tmp_path / "run" / "q-core.env"
    env_file.parent.mkdir()
    env_file.write_text("Q_CORE_API_TOKEN=invented\n")
    settings = test_settings.model_copy(update={
        "environment_file": str(env_file), "attachment_roots": ("/",),
    })
    with pytest.raises(ToolError) as excinfo:
        _attach(call_tool, settings, env_file)
    assert "environment file" in str(excinfo.value)
    assert "invented" not in str(excinfo.value)


def test_the_redaction_profile_is_refused_wherever_it_is(test_settings, call_tool, tmp_path):
    profile = tmp_path / "elsewhere" / "redaction.json"
    profile.parent.mkdir()
    profile.write_text('{"names": ["Invented Person"]}\n')
    settings = test_settings.model_copy(update={
        "privacy_profile_path": str(profile), "attachment_roots": ("/",),
    })
    with pytest.raises(ToolError) as excinfo:
        _attach(call_tool, settings, profile)
    assert "redaction profile" in str(excinfo.value)


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="no /proc on this platform")
def test_proc_is_refused_with_the_root_set_to_everything(test_settings, call_tool):
    settings = test_settings.model_copy(update={"attachment_roots": ("/",)})
    with pytest.raises(ToolError) as excinfo:
        _attach(call_tool, settings, Path("/proc/self/environ"))
    assert "/proc" in str(excinfo.value)


@pytest.mark.parametrize("hole", ["secrets", "backups"])
def test_secrets_and_backups_are_refused_when_they_live_outside_data_dir(test_settings, call_tool, tmp_path, hole):
    """The dedicated holes, not the data-directory rule, refuse these."""
    data = tmp_path / "srv" / "data"
    data.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    secrets_dir = elsewhere / "secrets"
    db_dir = elsewhere / "db"
    target = (secrets_dir / "google-refresh-token") if hole == "secrets" else (db_dir / "backups" / "q-core-2026-09-25T02-30-00Z.db")
    target.parent.mkdir(parents=True)
    target.write_text("invented, not a real secret\n")
    settings = test_settings.model_copy(update={
        "data_dir": str(data), "secrets_dir": str(secrets_dir),
        "db_path": str(db_dir / "q-core.db"), "attachment_roots": ("/",),
    })
    with pytest.raises(ToolError) as excinfo:
        _attach(call_tool, settings, target)
    assert "will not read" in str(excinfo.value)
    assert "invented" not in str(excinfo.value)
