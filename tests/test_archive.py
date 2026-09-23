"""Tests for the MCP server's retrieval layer.

No lei here: every test either intercepts subprocess.run to inspect the
argv we would have run, or feeds Archive._run canned output.  That is not
a shortcut around an integration test -- the two things most worth
pinning are exactly the ones a live archive would not catch.  Whether the
query was restricted to our own external is invisible in the results, and
the argv trap (a term with a boolean operator in it) fails by returning
nothing at all, which looks identical to an archive that simply has no
match.
"""

import datetime
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from archive import (
    ALL_DATES,
    MAX_LIMIT,
    SCAN_CAP,
    Archive,
    ArchiveError,
    Coverage,
    Hit,
    build_terms,
    group_threads,
)

# One line of `lei q -f jsonl', with lei's own short key names.
SAMPLE = {
    'm': 'patch-1@example.com',
    's': '[PATCH v2 1/2] usb: fix the thing',
    'f': [['Ann Submitter', 'ann@example.com']],
    'dt': '2026-09-01T10:00:00Z',
    'rt': '2026-09-01T10:00:05Z',
    'refs': ['cover@example.com'],
    'pct': 97,
}


def jsonl(*records: Dict[str, Any]) -> bytes:
    """Render records the way lei writes them: one JSON object per line."""
    return ''.join(json.dumps(record) + '\n' for record in records).encode()


class FakeRun:
    """Stands in for subprocess.run, recording argv and replaying output."""

    def __init__(self, stdout: bytes = b'', returncode: int = 0, stderr: bytes = b'') -> None:
        self.calls: List[List[str]] = []
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, cmd: List[str], **kwargs: Any) -> 'subprocess.CompletedProcess[bytes]':
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


@pytest.fixture
def archive(tmp_path: Path) -> Archive:
    """An Archive over a plausible extindex path, with coverage precomputed.

    Coverage is seeded rather than queried so a test about searching does
    not also have to satisfy the two queries coverage() would otherwise
    run; the tests that care about coverage clear it.
    """
    extindex = tmp_path / 'extindex'
    # It has to exist: a missing extindex is its own answer now, and a
    # test about searching should be testing the search.
    extindex.mkdir()
    arc = Archive(extindex=extindex, config=tmp_path / 'config')
    arc._coverage = {(str(extindex),): (float('inf'), Coverage(external=str(extindex)))}
    return arc


# --- query building ----------------------------------------------------


def test_each_filter_becomes_its_own_argument() -> None:
    """One term per argv item is the contract the whole module rests on."""
    terms = build_terms(sender='ann@example.com', subject='usb')
    assert terms == ['f:ann@example.com', 's:usb']


def test_a_boolean_inside_one_term_is_refused() -> None:
    """The failure that returns zero rows, exits 0 and warns nobody.

    lei reads a single argv containing spaces as a phrase, so this would
    search for the literal string rather than ANDing two terms.  Silent
    wrong answers are worth an exception.
    """
    with pytest.raises(ValueError, match='boolean operator'):
        build_terms(extra=['f:ann@example.com AND s:usb'])


def test_a_subject_phrase_is_still_allowed() -> None:
    """Spaces are fine; it is the operators that are not."""
    assert build_terms(subject='fix the thing') == ['s:fix the thing']


def test_dates_become_one_received_range() -> None:
    assert build_terms(since='20260301', until='20260401') == ['rt:2026-03-01T00:00:00Z..2026-04-01T23:59:59Z']


def test_an_open_ended_range_keeps_its_dots() -> None:
    """`rt:2026-03-01T00:00:00Z..' means "since then", the common case."""
    assert build_terms(since='20260301') == ['rt:2026-03-01T00:00:00Z..']


def test_a_date_is_always_emitted_with_dashes() -> None:
    """The bug this guards is silent in both directions.

    `rt:' is a numeric range over Unix timestamps, so a bare `20260917'
    is read as 20,260,917 seconds -- 1970-08-23.  As a lower bound that
    matches the whole archive and looks like a working filter; as an
    upper bound it matches nothing and looks like an empty archive.
    Neither warns.  Measured on a live archive:
    `rt:20260917..' returned every message and `rt:..20260918' returned
    none, while `rt:2026-09-16..2026-09-18' correctly returned a subset.
    """
    assert build_terms(since='20260917') == ['rt:2026-09-17T00:00:00Z..']
    assert build_terms(until='20260918') == ['rt:..2026-09-18T23:59:59Z']
    for term in build_terms(since='20260301', until='20260401'):
        assert '-' in term.split(':', 1)[1]


def test_a_date_names_a_whole_day_at_both_ends() -> None:
    """A date is not a moment, and lei resolves one with approxidate.

    Under approxidate the *current* date means now, not this morning, so
    a bare `rt:2026-09-17..' asked on the evening of the 17th behaves
    like `rt:today..' and answers nothing -- measured on a live archive
    holding mail received at 19:48 that same day.  Spelling the time out
    makes "what came in today" work, and makes *until* include the day
    it names instead of stopping at its midnight.
    """
    assert build_terms(since='20260917') == ['rt:2026-09-17T00:00:00Z..']
    assert build_terms(until='20260917') == ['rt:..2026-09-17T23:59:59Z']
    (one_day,) = build_terms(since='20260917', until='20260917')
    assert one_day == 'rt:2026-09-17T00:00:00Z..2026-09-17T23:59:59Z'


def test_a_date_already_dashed_is_taken_as_is() -> None:
    """Clients that read ISO dates off a previous result should not trip."""
    assert build_terms(since='2026-03-01') == ['rt:2026-03-01T00:00:00Z..']


def test_something_that_is_not_a_date_is_refused() -> None:
    """Better than letting it through to become a timestamp nobody meant."""
    for bad in ('last week', '2026', '20260231', '20261301', 'now'):
        with pytest.raises(ValueError, match='not a date'):
            build_terms(since=bad)


def test_every_named_filter_maps_to_its_public_inbox_prefix() -> None:
    """The whole point of the named parameters: nobody has to know `dfb'.

    Spelled out one by one rather than looped over the same table the
    code uses, because a test that shares the mapping under test only
    proves the loop runs.  These are from PublicInbox::Search's @HELP.
    """
    assert build_terms(sender='ann@example.com') == ['f:ann@example.com']
    assert build_terms(subject='usb') == ['s:usb']
    assert build_terms(nonquoted='Tested-by') == ['nq:Tested-by']
    assert build_terms(to='ann@example.com') == ['t:ann@example.com']
    assert build_terms(cc='ann@example.com') == ['c:ann@example.com']
    assert build_terms(participant='ann@example.com') == ['a:ann@example.com']
    assert build_terms(list_id='linux-usb.vger.kernel.org') == ['l:linux-usb.vger.kernel.org']
    assert build_terms(filename='config.gz') == ['n:config.gz']
    assert build_terms(subject_or_body='regression') == ['bs:regression']
    assert build_terms(diff_path='drivers/usb/core/hub.c') == ['dfn:drivers/usb/core/hub.c']
    assert build_terms(diff_added='kfree(urb)') == ['dfb:kfree(urb)']
    assert build_terms(diff_removed='kfree(urb)') == ['dfa:kfree(urb)']
    assert build_terms(diff_hunk='hub_port_init') == ['dfhh:hub_port_init']
    assert build_terms(patch_id='deadbeef') == ['patchid:deadbeef']


def test_filters_of_every_kind_combine_into_one_argv() -> None:
    """Headers, diff innards and a date range are all just ANDed terms."""
    terms = build_terms(
        participant='ann@example.com',
        diff_path='drivers/usb/',
        since='20260301',
    )
    assert terms == ['a:ann@example.com', 'dfn:drivers/usb/', 'rt:2026-03-01T00:00:00Z..']


def test_extra_passes_a_term_through_untouched() -> None:
    """The escape hatch for prefixes that have no parameter of their own."""
    assert build_terms(extra=['dfblob:1a2b3c', 'tc:ann@example.com']) == [
        'dfblob:1a2b3c',
        'tc:ann@example.com',
    ]


def test_an_exclusion_survives_the_round_trip() -> None:
    """Xapian runs with FLAG_LOVEHATE, so a leading `-' excludes.

    It has to stay one space-free token to get that far, which is exactly
    why exclusion lives in *extra* and is not a `-' on a named filter: a
    subject really can start with a dash (`-Wmaybe-uninitialized'), and
    silently inverting that search would be worse than no exclusion.
    """
    terms = build_terms(subject='usb', extra=['-f:syzbot@syzkaller.appspotmail.com'])
    assert terms == ['s:usb', '-f:syzbot@syzkaller.appspotmail.com']
    assert ' ' not in terms[1]


def test_a_dash_in_a_named_value_is_just_a_dash() -> None:
    """`-Wmaybe-uninitialized' is a real subject line, not an exclusion."""
    assert build_terms(subject='-Wmaybe-uninitialized') == ['s:-Wmaybe-uninitialized']


def test_an_empty_filter_adds_no_term() -> None:
    """Empty strings come back from clients that fill every field in."""
    assert build_terms(subject='usb', to='', diff_path=None) == ['s:usb']


def test_a_term_that_is_only_an_operator_is_the_join_and_is_allowed() -> None:
    """The guard must not eat the correct spelling of an OR.

    lei hands every argv to Xapian's parser, so an operator on its own
    joins its neighbours: dfn:a OR dfn:b as three arguments really is a
    union, while the same thing in one argument is a phrase nobody wrote.
    Rejecting the operator left no way to ask for a union at all.
    """
    assert build_terms(extra=['dfn:mm/filemap.c', 'OR', 'dfn:mm/truncate.c']) == [
        'dfn:mm/filemap.c',
        'OR',
        'dfn:mm/truncate.c',
    ]


def test_an_operator_buried_in_a_term_is_still_refused() -> None:
    """The trap itself: one argv with an operator inside matches nothing, quietly."""
    with pytest.raises(ValueError):
        build_terms(extra=['dfn:mm/filemap.c OR dfn:mm/truncate.c'])


# --- what we actually run ----------------------------------------------


def test_every_query_is_pinned_to_our_own_external(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """A query must answer from this container's archive and nothing else.

    -O restricts the search to that one external and, per LeiQuery.pm,
    also drops lei's own store ("--local is enabled by default unless
    --only is used"). --no-remote says the rest out loud.
    """
    fake = FakeRun(stdout=jsonl(SAMPLE))
    monkeypatch.setattr(subprocess, 'run', fake)

    archive.search(['s:usb'])

    cmd = fake.calls[0]
    assert cmd[:2] == ['lei', 'q']
    assert '-O' in cmd and cmd[cmd.index('-O') + 1] == str(archive.extindex)
    assert '--no-remote' in cmd
    assert cmd[-1] == 's:usb'


def test_a_query_can_be_pointed_at_a_different_archive(
    archive: Archive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Narrowing to a subsystem is choosing a corpus, not filtering results.

    korgalore delivers each tracked subsystem's mail into its own v2
    archive, indexed the same way the extindex is, so asking one is the
    same query over far less mail -- and the extindex is not consulted.
    """
    store = tmp_path / 'page_cache-patches'
    store.mkdir()
    fake = FakeRun(stdout=jsonl(SAMPLE))
    monkeypatch.setattr(subprocess, 'run', fake)

    archive.search(['s:usb'], externals=[store])

    cmd = fake.calls[0]
    assert [cmd[i + 1] for i, arg in enumerate(cmd) if arg == '-O'] == [str(store)]


def test_several_archives_are_all_named(archive: Archive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A maintainer with three subsystems asks once, not three times."""
    stores = []
    for name in ('page_cache-patches', 'xarray-patches'):
        store = tmp_path / name
        store.mkdir()
        stores.append(store)
    fake = FakeRun(stdout=jsonl(SAMPLE))
    monkeypatch.setattr(subprocess, 'run', fake)

    archive.search(['s:usb'], externals=stores)

    cmd = fake.calls[0]
    assert [cmd[i + 1] for i, arg in enumerate(cmd) if arg == '-O'] == [str(s) for s in stores]


def test_an_archive_that_is_not_built_yet_is_never_queried(
    archive: Archive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """korgalore creates a subsystem's archive with its first message.

    Until then the path is absent, and lei answers that with a warning
    about a stale external and exit 1 -- which reads like a broken
    install rather than a subsystem nobody has mailed about yet.
    """
    fake = FakeRun(stdout=jsonl(SAMPLE))
    monkeypatch.setattr(subprocess, 'run', fake)

    found = archive.search(['s:usb'], externals=[tmp_path / 'never-arrived'])

    assert fake.calls == []
    assert found.hits == ()


def test_a_scan_can_be_narrowed_the_same_way(archive: Archive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Thread mode counts through scan(), so it has to narrow too."""
    store = tmp_path / 'page_cache-patches'
    store.mkdir()
    fake = FakeRun(stdout=jsonl(SAMPLE))
    monkeypatch.setattr(subprocess, 'run', fake)

    archive.search_threads(['s:usb'], externals=[store])

    assert str(store) in fake.calls[0]


def test_a_narrowed_answer_reports_the_archive_it_came_from(
    archive: Archive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A subsystem archive holds a shorter window than the extindex.

    Reporting the whole archive's dates for a search of one subsystem's
    would overstate what an empty answer rules out.
    """
    store = tmp_path / 'page_cache-patches'
    store.mkdir()
    monkeypatch.setattr(subprocess, 'run', FakeRun(stdout=jsonl(SAMPLE)))

    coverage = archive.coverage([store]).to_dict()

    assert coverage['searched'] == str(store)
    assert coverage['inboxes'] == ['page_cache-patches']


def test_each_archive_is_remembered_on_its_own(
    archive: Archive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One cache for everything would hand a subsystem the extindex's dates."""
    store = tmp_path / 'page_cache-patches'
    store.mkdir()
    monkeypatch.setattr(subprocess, 'run', FakeRun(stdout=jsonl(SAMPLE)))

    archive.coverage([store])

    assert archive.coverage().to_dict()['searched'] == str(archive.extindex)


def test_terms_are_separated_from_lei_own_options(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--' between the switches and the search, or exclusions break.

    Xapian spells exclusion with a leading `-', and lei's own option
    parser gets the argv first: `-f:gregkh@example.com' is taken as
    `--format=:gregkh@example.com' and the query dies with "bad mail
    --format", while `-s:usb' becomes an unrecognised `--sort'.  Neither
    fails loudly at our end -- the tool just answers nothing and looks
    like an archive with no matches.
    """
    fake = FakeRun(stdout=jsonl(SAMPLE))
    monkeypatch.setattr(subprocess, 'run', fake)

    archive.search(['s:usb', '-f:gregkh@example.com'])

    cmd = fake.calls[0]
    sep = cmd.index('--')
    assert cmd[sep + 1 :] == ['s:usb', '-f:gregkh@example.com']
    # Every switch stays on lei's side of it.
    assert '-f' in cmd[:sep] and 'jsonl' in cmd[:sep]


def test_a_thread_fetch_separates_its_terms_too(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mboxrd callers pass switches of their own; same rule applies."""
    fake = FakeRun(stdout=b'')
    monkeypatch.setattr(subprocess, 'run', fake)

    archive.thread('p@example.com')

    cmd = fake.calls[0]
    assert cmd[cmd.index('--') + 1 :] == ['m:p@example.com']


def test_a_query_never_becomes_a_saved_search(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-save, because this container syncs its saved searches forever.

    kgl-pull-loop.sh refreshes every saved search on a timer. A question
    somebody asked once must not turn into one the container then keeps
    pulling from upstream.
    """
    fake = FakeRun(stdout=jsonl(SAMPLE))
    monkeypatch.setattr(subprocess, 'run', fake)

    archive.search(['s:usb'])

    assert '--no-save' in fake.calls[0]


def test_lei_failing_is_reported_not_swallowed(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun(returncode=1, stderr=b'no external found')
    monkeypatch.setattr(subprocess, 'run', fake)

    with pytest.raises(ArchiveError, match='no external found'):
        archive.search(['s:usb'])


def test_a_missing_lei_says_so(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cmd: List[str], **kwargs: Any) -> None:
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess, 'run', boom)

    with pytest.raises(ArchiveError, match='not found'):
        archive.search(['s:usb'])


# --- results -----------------------------------------------------------


def test_a_hit_reads_leis_short_key_names() -> None:
    hit = Hit.from_json(SAMPLE)
    assert hit.msgid == 'patch-1@example.com'
    assert hit.author == ('Ann Submitter', 'ann@example.com')
    assert hit.date == datetime.datetime(2026, 9, 1, 10, 0, tzinfo=datetime.timezone.utc)
    assert hit.refs == ('cover@example.com',)
    assert hit.relevance == 97


def test_a_from_without_a_display_name_is_ordinary() -> None:
    """lei writes null for the name half, which is not an error."""
    hit = Hit.from_json({**SAMPLE, 'f': [[None, 'ann@example.com']]})
    assert hit.author == ('', 'ann@example.com')


def test_one_unparsable_line_does_not_lose_the_rest(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, 'run', FakeRun(stdout=b'{ not json\n' + jsonl(SAMPLE)))

    page = archive.search(['s:usb'])

    assert [hit.msgid for hit in page.hits] == ['patch-1@example.com']


def test_a_full_page_says_it_was_truncated(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """Asking for one more row than wanted is how "there was more" is known."""
    records = [{**SAMPLE, 'm': f'msg-{n}@example.com'} for n in range(4)]
    monkeypatch.setattr(subprocess, 'run', FakeRun(stdout=jsonl(*records)))

    page = archive.search(['s:usb'], limit=3)

    assert len(page.hits) == 3
    assert page.truncated is True
    assert page.to_dict()['truncated'] is True


def test_a_short_page_does_not_claim_it_was(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, 'run', FakeRun(stdout=jsonl(SAMPLE)))

    page = archive.search(['s:usb'], limit=3)

    assert page.truncated is False


def test_the_limit_asks_lei_for_one_extra(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun(stdout=jsonl(SAMPLE))
    monkeypatch.setattr(subprocess, 'run', fake)

    archive.search(['s:usb'], limit=25)

    cmd = fake.calls[0]
    assert cmd[cmd.index('-n') + 1] == '26'


def test_an_impossible_limit_is_refused(archive: Archive) -> None:
    with pytest.raises(ValueError, match='at least 1'):
        archive.search(['s:usb'], limit=0)
    with pytest.raises(ValueError, match=str(MAX_LIMIT)):
        archive.search(['s:usb'], limit=MAX_LIMIT + 1)


def test_a_search_with_no_terms_is_refused(archive: Archive) -> None:
    """lei errors on an empty query; say why before spending a subprocess."""
    with pytest.raises(ValueError, match='at least one term'):
        archive.search([])


# --- threads -----------------------------------------------------------


MBOX = b"""From mboxrd@z Thu Jan  1 00:00:00 1970
From: Ann Submitter <ann@example.com>
Subject: [PATCH] usb: fix the thing
Message-Id: <patch-1@example.com>
Date: Tue, 1 Sep 2026 10:00:00 +0000

Body of the patch.

From mboxrd@z Thu Jan  1 00:00:00 1970
From: Sashiko Reviewer <sashiko@example.org>
Subject: Re: [PATCH] usb: fix the thing
Message-Id: <reply-1@example.com>
In-Reply-To: <patch-1@example.com>
Date: Tue, 1 Sep 2026 11:00:00 +0000

Reviewed-by: Sashiko Reviewer <sashiko@example.org>
"""


def test_a_thread_comes_back_summarised(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, 'run', FakeRun(stdout=MBOX))

    summary = archive.thread('patch-1@example.com')

    assert summary is not None
    assert summary.count == 2
    assert summary.author == ('Ann Submitter', 'ann@example.com')
    assert 'reviewed-by' in summary.trailer_names


def test_a_thread_is_fetched_by_message_id_with_expansion(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """-t expands server-side, so one command replaces a reference walk."""
    fake = FakeRun(stdout=MBOX)
    monkeypatch.setattr(subprocess, 'run', fake)

    archive.thread('<patch-1@example.com>')

    cmd = fake.calls[0]
    assert '-t' in cmd
    assert cmd[-1] == 'm:patch-1@example.com'


def test_a_thread_we_do_not_have_is_an_answer_not_an_error(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordinary case for a partial mirror -- and the reason coverage exists."""
    monkeypatch.setattr(subprocess, 'run', FakeRun(stdout=b''))

    assert archive.thread('nothing@example.com') is None


class ReplayRun:
    """subprocess.run that answers each call from a queue of outputs.

    threads() runs two commands with two different output formats, so one
    canned stdout will not do.
    """

    def __init__(self, *outputs: bytes) -> None:
        self.calls: List[List[str]] = []
        self.outputs = list(outputs)

    def __call__(self, cmd: List[str], **kwargs: Any) -> 'subprocess.CompletedProcess[bytes]':
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, self.outputs.pop(0), b'')


def test_threads_returns_summaries_and_what_was_searched(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, 'run', ReplayRun(jsonl(SAMPLE), MBOX))

    summaries, page = archive.threads(['s:usb'], limit=5)

    assert [summary.subject for summary in summaries] == ['[PATCH] usb: fix the thing']
    assert 'reviewed-by' in summaries[0].trailer_names
    assert page.terms == ('s:usb',)


def test_threads_asks_the_header_pass_the_same_question(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two commands, same terms: one answers honestly, the other reads."""
    fake = ReplayRun(jsonl(SAMPLE), MBOX)
    monkeypatch.setattr(subprocess, 'run', fake)

    archive.threads(['s:usb'], limit=5)

    headers, bodies = fake.calls
    assert headers[-1] == 's:usb'
    assert bodies[-1] == 's:usb'
    assert '-f' in headers and headers[headers.index('-f') + 1] == 'jsonl'
    assert '-f' in bodies and bodies[bodies.index('-f') + 1] == 'mboxrd'
    assert '-t' in bodies


def test_threads_truncation_comes_from_the_header_pass(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """Messages -t drags in do not count towards --limit, so they cannot answer this.

    Two hits for a limit of one: the header pass saw the cap, and says so,
    even though the mbox that follows is one thread either way.
    """
    monkeypatch.setattr(subprocess, 'run', ReplayRun(jsonl(SAMPLE, SAMPLE), MBOX))

    _, page = archive.threads(['s:usb'], limit=1)

    assert page.truncated is True
    assert len(page.hits) == 1


def test_threads_with_no_match_is_an_empty_answer(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, 'run', ReplayRun(b'', b''))

    summaries, page = archive.threads(['s:nothing'])

    assert summaries == []
    assert page.hits == ()


# --- an archive that does not exist yet ---------------------------------


@pytest.fixture
def unbuilt(tmp_path: Path) -> Archive:
    """An Archive pointed at an extindex that has not been built yet.

    The state every container is in before its first subsystem is tracked,
    and the one lei is least helpful about.
    """
    return Archive(extindex=tmp_path / 'extindex', config=tmp_path / 'config')


def test_an_unbuilt_archive_is_never_queried(unbuilt: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """lei answers this with exit 1 and advice to forget a stale external.

    Neither is true -- the external is not stale, it has not been built --
    so the question is not asked at all.
    """
    fake = FakeRun()
    monkeypatch.setattr(subprocess, 'run', fake)

    page = unbuilt.search(['s:usb'])

    assert fake.calls == []
    assert page.hits == ()
    assert page.truncated is False


def test_an_unbuilt_archive_says_it_is_not_built(unbuilt: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty result has to be distinguishable from "nothing matched"."""
    monkeypatch.setattr(subprocess, 'run', FakeRun())

    found = unbuilt.search(['s:usb']).coverage.to_dict()

    assert found['ready'] is False
    assert 'not been built yet' in found['note']


def test_a_built_archive_says_so(archive: Archive) -> None:
    found = archive.coverage().to_dict()
    assert found['ready'] is True
    assert 'not been built yet' not in found['note']


def test_an_unbuilt_archive_has_no_threads(unbuilt: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, 'run', FakeRun())
    assert unbuilt.thread('p@example.com') is None
    summaries, _ = unbuilt.threads(['s:usb'])
    assert summaries == []


def test_not_built_yet_is_not_cached(unbuilt: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """The container grows out of this state, often while somebody watches.

    Caching it would keep answering "not built" for the rest of the TTL
    after the first index finished.
    """
    monkeypatch.setattr(subprocess, 'run', FakeRun())
    assert unbuilt.coverage().ready is False

    unbuilt.extindex.mkdir()
    monkeypatch.setattr(subprocess, 'run', FakeRun(stdout=jsonl(SAMPLE)))

    assert unbuilt.coverage().ready is True


# --- coverage ----------------------------------------------------------


def test_coverage_reports_the_window_the_archive_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    oldest = {**SAMPLE, 'rt': '2026-03-01T00:00:00Z'}
    newest = {**SAMPLE, 'rt': '2026-09-17T00:00:00Z'}
    calls: List[List[str]] = []

    def fake_run(cmd: List[str], **kwargs: Any) -> 'subprocess.CompletedProcess[bytes]':
        calls.append(cmd)
        record = oldest if '-r' in cmd else newest
        return subprocess.CompletedProcess(cmd, 0, jsonl(record), b'')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    (tmp_path / 'extindex').mkdir()
    arc = Archive(extindex=tmp_path / 'extindex')

    found = arc.coverage()

    assert found.earliest == datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)
    assert found.latest == datetime.datetime(2026, 9, 17, tzinfo=datetime.timezone.utc)
    assert all(ALL_DATES in cmd for cmd in calls)


def test_coverage_is_not_recomputed_for_every_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mail loop runs every ten minutes; this need not run every tool call."""
    fake = FakeRun(stdout=jsonl(SAMPLE))
    monkeypatch.setattr(subprocess, 'run', fake)
    (tmp_path / 'extindex').mkdir()
    arc = Archive(extindex=tmp_path / 'extindex')

    arc.coverage()
    arc.coverage()

    assert len(fake.calls) == 2  # oldest and newest, once -- not twice each


def test_coverage_survives_a_failing_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown window beats failing a search that would have worked."""
    monkeypatch.setattr(subprocess, 'run', FakeRun(returncode=1, stderr=b'nope'))
    (tmp_path / 'extindex').mkdir()
    arc = Archive(extindex=tmp_path / 'extindex')

    found = arc.coverage()

    assert found.earliest is None
    assert 'not that no such message exists' in found.to_dict()['note']


def test_coverage_names_the_inboxes_from_public_inbox_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git-config subsection syntax, which configparser cannot read."""
    config = tmp_path / 'config'
    config.write_text(
        '[publicinbox "usb-mailinglist"]\n'
        '\tinboxdir = /data/xdg/data/korgalore/lei/usb-mailinglist\n'
        '[publicinbox "usb-patches"]\n'
        '\tinboxdir = /data/xdg/data/korgalore/lei/usb-patches\n'
        '[extindex "all"]\n'
        '\ttopdir = /data/publicinbox/extindex\n'
    )
    monkeypatch.setattr(subprocess, 'run', FakeRun(stdout=b''))
    arc = Archive(extindex=tmp_path / 'extindex', config=config)

    assert arc.inboxes() == ('usb-mailinglist', 'usb-patches')


def test_a_missing_config_is_not_fatal(tmp_path: Path) -> None:
    """Coverage degrades to "we do not know"; it never takes a search down."""
    arc = Archive(extindex=tmp_path / 'extindex', config=tmp_path / 'absent')
    assert arc.inboxes() == ()


def test_every_result_carries_its_coverage(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """The caveat has to travel with the answer, not sit in a README."""
    monkeypatch.setattr(subprocess, 'run', FakeRun(stdout=jsonl(SAMPLE)))

    page = archive.search(['s:usb'])

    assert 'coverage' in page.to_dict()
    assert page.to_dict()['coverage']['searched'] == str(archive.extindex)


# --- paging ------------------------------------------------------------


def _rows(count: int, start: int = 1) -> List[Dict[str, Any]]:
    """`count' jsonl records, oldest last, the way lei sorts by received."""
    return [
        {
            'm': f'p{n}@example.com',
            's': f'[PATCH] number {n}',
            'f': [['Ann Submitter', 'ann@example.com']],
            'rt': f'2026-09-{n:02d}T00:00:00Z',
        }
        for n in range(start, start + count)
    ]


def test_a_page_asks_for_everything_up_to_its_end_plus_one(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """lei's own --offset is parsed and dropped, so the slicing is ours.

    Asking for one past the end of the window is what makes `truncated'
    an observation rather than a guess about a page that came back full.
    """
    fake = FakeRun(stdout=jsonl(*_rows(40)))
    monkeypatch.setattr(subprocess, 'run', fake)

    archive.search(['s:usb'], limit=10, offset=20)

    assert '-n' in fake.calls[0]
    assert fake.calls[0][fake.calls[0].index('-n') + 1] == '31'


def test_a_page_starts_where_the_offset_says(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, 'run', FakeRun(stdout=jsonl(*_rows(40))))

    page = archive.search(['s:usb'], limit=3, offset=5)

    assert [hit.msgid for hit in page.hits] == [
        'p6@example.com',
        'p7@example.com',
        'p8@example.com',
    ]
    assert page.offset == 5


def test_a_page_in_the_middle_points_at_the_next_one(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, 'run', FakeRun(stdout=jsonl(*_rows(40))))

    page = archive.search(['s:usb'], limit=10, offset=10)

    assert page.truncated is True
    assert page.next_offset == 20


def test_the_last_page_says_it_is_the_last(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller loops until next_offset is None, so it has to become None."""
    monkeypatch.setattr(subprocess, 'run', FakeRun(stdout=jsonl(*_rows(12))))

    page = archive.search(['s:usb'], limit=10, offset=10)

    assert [hit.msgid for hit in page.hits] == ['p11@example.com', 'p12@example.com']
    assert page.truncated is False
    assert page.next_offset is None


def test_a_negative_offset_is_refused(archive: Archive) -> None:
    with pytest.raises(ValueError, match='offset'):
        archive.search(['s:usb'], offset=-1)


def test_paging_past_the_scan_cap_is_refused(archive: Archive) -> None:
    """The re-fetched prefix is the cost of lei's broken --offset; bound it."""
    with pytest.raises(ValueError, match='at most'):
        archive.search(['s:usb'], limit=10, offset=SCAN_CAP)


# --- counting ----------------------------------------------------------


def test_a_scan_walks_everything_and_says_it_was_complete(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, 'run', FakeRun(stdout=jsonl(*_rows(30))))

    found = archive.scan(['s:usb'], cap=100)

    assert len(found.hits) == 30
    assert found.complete is True
    assert 'real' in found.to_dict()['note']


def test_a_scan_that_hits_its_cap_reports_a_lower_bound(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly `cap' rows is ambiguous, which is why cap+1 is asked for."""
    monkeypatch.setattr(subprocess, 'run', FakeRun(stdout=jsonl(*_rows(11))))

    found = archive.scan(['s:usb'], cap=10)

    assert len(found.hits) == 10
    assert found.complete is False
    assert 'lower bounds' in found.to_dict()['note']


def test_a_scan_of_an_unbuilt_archive_is_never_queried(unbuilt: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun(stdout=b'')
    monkeypatch.setattr(subprocess, 'run', fake)

    found = unbuilt.scan(['s:usb'])

    assert found.hits == ()
    assert fake.calls == []


# --- grouping ----------------------------------------------------------


def _hit(msgid: str, refs: Sequence[str] = (), day: int = 1) -> Hit:
    return Hit(
        msgid=msgid,
        subject='[PATCH] thing',
        author=('Ann', 'ann@example.com'),
        date=None,
        received=datetime.datetime(2026, 9, day, tzinfo=datetime.timezone.utc),
        refs=tuple(refs),
    )


def test_a_message_with_no_references_is_its_own_thread_root() -> None:
    assert _hit('root@example.com').root == 'root@example.com'


def test_a_reply_belongs_to_the_thread_its_references_name() -> None:
    """refs[0] is the oldest ancestor, which is the root by definition."""
    reply = _hit('reply@example.com', refs=('root@example.com', 'mid@example.com'))
    assert reply.root == 'root@example.com'


def test_replies_fold_into_one_group_with_their_root() -> None:
    groups = group_threads(
        [
            _hit('root@example.com', day=1),
            _hit('a@example.com', refs=('root@example.com',), day=2),
            _hit('b@example.com', refs=('root@example.com',), day=3),
        ]
    )

    assert len(groups) == 1
    assert groups[0].root == 'root@example.com'
    assert groups[0].last is not None
    assert groups[0].last.day == 3


def test_groups_come_back_most_recently_active_first() -> None:
    groups = group_threads(
        [
            _hit('quiet@example.com', day=1),
            _hit('busy@example.com', day=9),
        ]
    )

    assert [group.root for group in groups] == ['busy@example.com', 'quiet@example.com']


def test_a_group_is_anchored_on_a_message_the_archive_really_holds() -> None:
    """The root may predate the mirror window; a hit never does.

    Asking lei for `m:<root>' when the root was never mirrored finds
    nothing, and the whole thread would silently vanish from a listing.
    """
    groups = group_threads([_hit('reply@example.com', refs=('ancient@example.com',))])

    assert groups[0].root == 'ancient@example.com'
    assert groups[0].anchor == 'reply@example.com'


def test_a_group_names_everyone_who_wrote_in_it_once_each() -> None:
    """Distinct authors of the matched messages, oldest first."""
    ann = _hit('a@example.com', day=1)
    bob = Hit(
        msgid='b@example.com',
        subject='Re: [PATCH] thing',
        author=('Bob Reviewer', 'bob@example.net'),
        date=None,
        received=datetime.datetime(2026, 9, 2, tzinfo=datetime.timezone.utc),
        refs=('a@example.com',),
    )
    again = _hit('c@example.com', refs=('a@example.com',), day=3)

    group = group_threads([ann, bob, again])[0]
    assert [email for _, email in group.authors] == ['ann@example.com', 'bob@example.net']


def test_a_group_serialises_the_fields_a_caller_triages_on() -> None:
    group = group_threads([_hit('reply@example.com', refs=('ancient@example.com',), day=4)])[0]
    found = group.to_dict()

    assert found['root'] == 'ancient@example.com'
    assert found['msgid'] == 'reply@example.com'
    assert found['subject'] == '[PATCH] thing'
    assert found['count'] == 1
    assert found['last'].startswith('2026-09-04')
    assert found['authors'] == [{'name': 'Ann', 'email': 'ann@example.com'}]


# --- searching by thread -----------------------------------------------


def _many(*specs: Tuple[str, Sequence[str], int]) -> bytes:
    """lei output for (msgid, refs, day) triples, as one jsonl blob."""
    return jsonl(
        *(
            {
                'm': msgid,
                's': '[PATCH] thing',
                'f': [['Ann', 'ann@example.com']],
                'rt': f'2026-09-{day:02d}T00:00:00Z',
                'refs': list(refs),
            }
            for msgid, refs, day in specs
        )
    )


def test_a_thread_search_answers_in_threads_not_messages(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        'run',
        FakeRun(
            stdout=_many(
                ('cover@example.com', (), 1),
                ('one@example.com', ('cover@example.com',), 2),
                ('two@example.com', ('cover@example.com',), 3),
            )
        ),
    )
    found = archive.search_threads(['s:PATCH']).to_dict()

    assert found['total'] == 1
    assert found['count'] == 1
    assert found['threads'][0]['count'] == 3


def test_a_thread_search_scans_rather_than_pages(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """A thread cannot be counted from a slice, so the walk is the whole set.

    If this asked lei for limit+1 rows the way search() does, `total'
    would be the number of threads in one page of messages, which is not
    a number anybody asked for.
    """
    fake = FakeRun(stdout=_many(('p@example.com', (), 1)))
    monkeypatch.setattr(subprocess, 'run', fake)
    archive.search_threads(['s:PATCH'], limit=5)

    assert f'{SCAN_CAP + 1}' in fake.calls[0]


def test_a_thread_search_counts_past_the_page_it_returns(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        'run',
        FakeRun(stdout=_many(*((f'p{n}@example.com', (), n) for n in range(1, 6)))),
    )
    page = archive.search_threads(['s:PATCH'], limit=2)

    assert page.total == 5
    assert len(page.groups) == 2
    assert page.next_offset == 2


def test_a_thread_search_offset_walks_the_thread_list(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        'run',
        FakeRun(stdout=_many(*((f'p{n}@example.com', (), n) for n in range(1, 6)))),
    )
    page = archive.search_threads(['s:PATCH'], limit=2, offset=4)

    assert [group.root for group in page.groups] == ['p1@example.com']
    assert page.next_offset is None


def test_a_capped_thread_search_says_its_total_is_a_lower_bound(
    archive: Archive, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        'run',
        FakeRun(stdout=_many(('p@example.com', (), 1), ('q@example.com', (), 2))),
    )
    found = archive.search_threads(['s:PATCH'], cap=1).to_dict()

    assert found['total'] == 1

    assert found['scan']['complete'] is False
    assert 'lower bounds' in found['scan']['note']


def test_an_impossible_thread_limit_is_refused(archive: Archive) -> None:
    with pytest.raises(ValueError):
        archive.search_threads(['s:PATCH'], limit=0)
    with pytest.raises(ValueError):
        archive.search_threads(['s:PATCH'], limit=MAX_LIMIT + 1)
    with pytest.raises(ValueError):
        archive.search_threads(['s:PATCH'], offset=-1)


def test_a_thread_search_with_no_terms_is_refused(archive: Archive) -> None:
    with pytest.raises(ValueError):
        archive.search_threads([])


# --- expanding a page --------------------------------------------------


def test_several_threads_are_expanded_in_one_query(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare OR has to be its own argv item.

    Search.pm's query_argv_to_string() quotes any argv item holding
    whitespace, so `m:a OR m:b' passed as one string is searched as a
    literal phrase and matches nothing at all.
    """
    fake = FakeRun(stdout=b'')
    monkeypatch.setattr(subprocess, 'run', fake)

    archive.summarize(['a@example.com', 'b@example.com', 'c@example.com'])

    assert fake.calls[0][-5:] == [
        'm:a@example.com',
        'OR',
        'm:b@example.com',
        'OR',
        'm:c@example.com',
    ]
    assert '-t' in fake.calls[0]


def test_expanding_more_than_a_chunk_takes_more_than_one_query(
    archive: Archive, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One stable-review posting can be a thousand patches on its own."""
    fake = FakeRun(stdout=b'')
    monkeypatch.setattr(subprocess, 'run', fake)

    archive.summarize([f'm{n}@example.com' for n in range(5)], chunk=2)

    assert len(fake.calls) == 3


def test_expanding_nothing_asks_lei_nothing(archive: Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun(stdout=b'')
    monkeypatch.setattr(subprocess, 'run', fake)

    assert archive.summarize([]) == []
    assert fake.calls == []


def test_expanding_strips_the_angle_brackets_a_model_will_send(
    archive: Archive, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun(stdout=b'')
    monkeypatch.setattr(subprocess, 'run', fake)

    archive.summarize(['<a@example.com>'])

    assert fake.calls[0][-1] == 'm:a@example.com'
