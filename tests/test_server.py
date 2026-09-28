# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
"""Tests for the MCP server's tools.

The retrieval layer has its own tests; these are about what the tools do
with what it returns -- which questions they refuse, what they leave out
of a listing, and whether an answer carries the coverage caveat with it.
So Archive is stubbed here rather than lei, and no query is ever built.

What is worth pinning is mostly about honesty: a maintainer with no
identity configured must get an explanation rather than an empty list
that reads like good news, and every answer must say what archive it came
from.
"""

import datetime
import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pytest
from liblore.series import ThreadSummary

import server
from archive import ALL_DATES, MAX_LIMIT, SCAN_CAP, Coverage, Hit, Page, Scan, ThreadPage
from subsystems import Subsystems

# What the two factory fixtures hand a test: call it, get the thing back.
Install = Callable[..., Any]

Person = Tuple[str, str]

COVERAGE = Coverage(
    external='/data/publicinbox/extindex',
    inboxes=('usb', 'staging'),
    earliest=datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc),
    latest=datetime.datetime(2026, 9, 17, tzinfo=datetime.timezone.utc),
)

ME = ('Mai Maintainer', 'mai@example.org')


def thread(
    msgid: str,
    *,
    author: Person = ('Ann Submitter', 'ann@example.com'),
    participants: Sequence[Person] = (),
    patch: bool = True,
    last: int = 1,
) -> ThreadSummary:
    """A ThreadSummary with only the fields these tools actually read."""
    return ThreadSummary(
        msgid=msgid,
        subject=f'[PATCH] {msgid}',
        count=1 + len(participants),
        first=datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc),
        last=datetime.datetime(2026, 9, last, tzinfo=datetime.timezone.utc),
        msgids=(msgid,),
        is_patch_series=patch,
        patch_count=1 if patch else 0,
        author=author,
        participants=(author, *participants),
        messages=(),
    )


def hit(
    msgid: str,
    *,
    subject: Optional[str] = None,
    sender: Person = ('Ann Submitter', 'ann@example.com'),
    refs: Sequence[str] = (),
    day: int = 1,
) -> Hit:
    """A header-only hit, which is all the counting scans ever see."""
    return Hit(
        msgid=msgid,
        subject=f'[PATCH] {msgid}' if subject is None else subject,
        author=sender,
        date=None,
        received=datetime.datetime(2026, 9, day, tzinfo=datetime.timezone.utc),
        refs=tuple(refs),
    )


class StubArchive:
    """Stands in for Archive, answering with whatever a test handed it.

    `hits' is what a scan finds, which is also what a thread-mode search
    is built out of; `page' is what a message-mode search hands back.
    """

    def __init__(
        self,
        *,
        hits: Sequence[Hit] = (),
        summaries: Sequence[ThreadSummary] = (),
        truncated: bool = False,
        complete: bool = True,
        terms: Sequence[str] = ('s:PATCH',),
    ) -> None:
        self.page = Page(hits=tuple(hits), limit=25, truncated=truncated, coverage=COVERAGE, terms=tuple(terms))
        self.hits = tuple(hits)
        self.summaries = list(summaries)
        self.complete = complete
        self.asked: List[Tuple[Any, ...]] = []
        self.searched: List[Tuple[Path, ...]] = []

    def search(self, terms: Sequence[str], limit: int = 25, offset: int = 0, externals: Sequence[Path] = ()) -> Page:
        self.asked.append(('search', list(terms), limit, offset))
        self.searched.append(tuple(externals))
        return self.page

    def scan(self, terms: Sequence[str], cap: int = SCAN_CAP, externals: Sequence[Path] = ()) -> Scan:
        self.asked.append(('scan', list(terms), cap))
        self.searched.append(tuple(externals))
        return Scan(
            hits=self.hits,
            cap=cap,
            complete=self.complete,
            coverage=COVERAGE,
            terms=tuple(terms),
        )

    def search_threads(
        self,
        terms: Sequence[str],
        limit: int = 25,
        offset: int = 0,
        cap: int = SCAN_CAP,
        externals: Sequence[Path] = (),
    ) -> ThreadPage:
        self.asked.append(('search_threads', list(terms), limit, offset))
        found = self.scan(terms, cap=cap, externals=externals)
        groups = found.groups()
        return ThreadPage(
            groups=tuple(groups[offset : offset + limit]),
            limit=limit,
            offset=offset,
            total=len(groups),
            scan=found,
        )

    def summarize(self, msgids: Iterable[str], chunk: int = 25) -> List[ThreadSummary]:
        self.asked.append(('summarize', list(msgids)))
        wanted = set(msgids)
        return [summary for summary in self.summaries if wanted.intersection(summary.msgids)]

    def thread(self, msgid: str) -> Optional[ThreadSummary]:
        self.asked.append(('thread', msgid))
        return self.summaries[0] if self.summaries else None

    def coverage(self, externals: Sequence[Path] = ()) -> Coverage:
        return COVERAGE


@pytest.fixture
def tracked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Install a real registry over two tracked subsystems.

    Real files rather than a stub registry: the point of these tests is
    that a name a maintainer says turns into the right archives, and the
    turning is done by reading what korgalore wrote.
    """
    conf_d = tmp_path / 'conf.d'
    conf_d.mkdir()
    base = tmp_path / 'lei'
    for key, name in (('page_cache', 'PAGE CACHE'), ('xarray', 'XARRAY')):
        (conf_d / f'{key}.toml').write_text(
            f"[subsystem]\nname = '{name}'\n"
            f"[feeds.{key}-mailinglist]\nurl = 'lei:{base}/{key}-mailinglist'\n"
            f"[feeds.{key}-patches]\nurl = 'lei:{base}/{key}-patches'\n"
        )
    registry = Subsystems(conf_d=conf_d, saved_searches=tmp_path / 'saved-searches')
    monkeypatch.setattr(server, 'SUBSYSTEMS', registry)
    return base


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Install:
    """Install a StubArchive and give the test something to load it up with."""

    def install(**kwargs: Any) -> StubArchive:
        archive = StubArchive(**kwargs)
        monkeypatch.setattr(server, 'ARCHIVE', archive)
        return archive

    return install


@pytest.fixture
def identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Install:
    """Write an identity.json where the server will look for it."""

    def install(value: Optional[Dict[str, Any]]) -> Path:
        path = tmp_path / 'identity.json'
        if value is not None:
            path.write_text(json.dumps(value), encoding='utf-8')
        monkeypatch.setattr(server, 'IDENTITY_PATH', path)
        return path

    return install


# --- searching ---------------------------------------------------------


def test_a_search_with_no_filters_is_refused(stub: Install) -> None:
    """Better than matching the whole archive and calling it an answer."""
    stub()
    assert 'error' in server.search_archive()


def test_a_boolean_smuggled_into_a_filter_is_refused(stub: Install) -> None:
    """The argv trap, arriving from a model rather than from our own code."""
    archive = stub()
    found = server.search_archive(sender='ann@example.com AND s:usb')
    assert 'boolean operator' in found['error']
    assert archive.asked == []


def test_a_search_answers_with_records_and_coverage(stub: Install) -> None:
    stub(
        hits=[
            Hit(
                msgid='p@example.com',
                subject='[PATCH] usb',
                author=('Ann', 'ann@example.com'),
                date=None,
                received=None,
            )
        ]
    )
    found = server.search_archive(subject='usb')
    assert [hit['msgid'] for hit in found['hits']] == ['p@example.com']
    assert found['coverage']['inboxes'] == ['usb', 'staging']


def test_the_new_filters_reach_lei_as_terms(stub: Install) -> None:
    """The tool's job here is plumbing, so check the plumbing connects."""
    archive = stub()
    server.search_archive(
        participant='ann@example.com',
        diff_path='drivers/usb/',
        diff_hunk='hub_port_init',
        list_id='linux-usb.vger.kernel.org',
    )
    assert archive.asked[0][1] == [
        'a:ann@example.com',
        'l:linux-usb.vger.kernel.org',
        'dfn:drivers/usb/',
        'dfhh:hub_port_init',
    ]


def test_extra_reaches_lei_and_can_exclude(stub: Install) -> None:
    archive = stub()
    server.search_archive(subject='usb', extra=['-f:syzbot@syzkaller.appspotmail.com'])
    assert archive.asked[0][1] == ['s:usb', '-f:syzbot@syzkaller.appspotmail.com']


def test_a_boolean_smuggled_through_extra_is_refused(stub: Install) -> None:
    """extra is a passthrough, not a hole in the one guard that matters."""
    archive = stub()
    found = server.search_archive(extra=['f:ann@example.com AND s:usb'])
    assert 'boolean operator' in found['error']
    assert archive.asked == []


def test_extra_alone_is_enough_of_a_filter(stub: Install) -> None:
    """A raw term is a real filter; the tool should not insist on a named one."""
    archive = stub()
    found = server.search_archive(extra=['dfblob:1a2b3c'])
    assert 'error' not in found
    assert archive.asked[0][1] == ['dfblob:1a2b3c']


def test_a_limit_beyond_our_own_is_clamped_not_refused(stub: Install) -> None:
    archive = stub()
    server.search_archive(subject='usb', limit=999999)
    assert archive.asked[0][2] == MAX_LIMIT


def test_a_search_pages_from_the_offset_it_was_given(stub: Install) -> None:
    archive = stub()
    server.search_archive(subject='usb', offset=50)
    assert archive.asked[0][3] == 50


def test_a_negative_offset_is_read_as_the_beginning(stub: Install) -> None:
    """A model that counts backwards should get page one, not an error."""
    archive = stub()
    server.search_archive(subject='usb', offset=-10)
    assert archive.asked[0][3] == 0


# --- one thread --------------------------------------------------------


def test_a_missing_thread_says_so_and_still_reports_coverage(stub: Install) -> None:
    """An empty answer from a partial mirror is not the same as "no such thread"."""
    stub()
    found = server.get_thread('nothing@example.com')
    assert found['not_found'] == 'nothing@example.com'
    assert found['coverage']['earliest'].startswith('2026-03-01')


def test_a_thread_comes_back_whole(stub: Install) -> None:
    stub(summaries=[thread('p@example.com')])
    found = server.get_thread('p@example.com')
    assert found['msgid'] == 'p@example.com'
    assert 'coverage' in found


# --- thread mode -------------------------------------------------------


def test_by_thread_folds_messages_onto_the_thread_they_belong_to(stub: Install) -> None:
    """Three messages of one series are one row, not three."""
    stub(
        hits=[
            hit('cover@example.com', subject='[PATCH 0/2] usb: fixes'),
            hit('one@example.com', subject='[PATCH 1/2] usb: first', refs=('cover@example.com',), day=2),
            hit('two@example.com', subject='[PATCH 2/2] usb: second', refs=('cover@example.com',), day=3),
        ]
    )
    found = server.search_archive(subject='PATCH', by_thread=True)
    assert found['total'] == 1
    assert found['threads'][0]['count'] == 3
    assert found['threads'][0]['root'] == 'cover@example.com'


def test_a_thread_row_names_a_message_this_archive_holds(stub: Install) -> None:
    """`root' may never have been mirrored; `msgid' always was."""
    stub(hits=[hit('reply@example.com', refs=('never-mirrored@example.com',))])
    row = server.search_archive(subject='PATCH', by_thread=True)['threads'][0]
    assert row['root'] == 'never-mirrored@example.com'
    assert row['msgid'] == 'reply@example.com'


def test_thread_mode_counts_every_thread_not_just_the_page(stub: Install) -> None:
    """The whole reason it scans instead of paging: a real total."""
    stub(hits=[hit(f'p{n}@example.com', day=n) for n in range(1, 6)])
    found = server.search_archive(subject='PATCH', by_thread=True, limit=2)
    assert found['total'] == 5
    assert found['count'] == 2
    assert found['next_offset'] == 2


def test_thread_mode_ends_its_listing_honestly(stub: Install) -> None:
    stub(hits=[hit(f'p{n}@example.com', day=n) for n in range(1, 6)])
    assert server.search_archive(subject='PATCH', by_thread=True, limit=2, offset=4)['next_offset'] is None


def test_thread_mode_lists_the_most_recently_active_first(stub: Install) -> None:
    stub(hits=[hit('old@example.com', day=1), hit('new@example.com', day=9)])
    found = server.search_archive(subject='PATCH', by_thread=True)
    assert [row['msgid'] for row in found['threads']] == ['new@example.com', 'old@example.com']


def test_thread_mode_says_when_its_count_is_a_lower_bound(stub: Install) -> None:
    """A capped scan means some threads were not seen at all."""
    stub(hits=[hit('p@example.com')], complete=False)
    found = server.search_archive(subject='PATCH', by_thread=True)
    assert found['scan']['complete'] is False
    assert 'lower bounds' in found['scan']['note']


def test_thread_mode_carries_the_coverage_caveat_too(stub: Install) -> None:
    stub(hits=[hit('p@example.com')])
    found = server.search_archive(subject='PATCH', by_thread=True)
    assert found['coverage']['inboxes'] == ['usb', 'staging']


def test_without_by_thread_the_answer_is_still_messages(stub: Install) -> None:
    """The default must not change under anyone who was already calling this."""
    archive = stub(hits=[hit('p@example.com')])
    found = server.search_archive(subject='PATCH')
    assert 'hits' in found and 'threads' not in found
    assert archive.asked[0][0] == 'search'


def test_the_two_modes_ask_the_same_question(stub: Install) -> None:
    """Only the shape of the answer differs, never the query."""
    archive = stub(hits=[hit('p@example.com')])
    server.search_archive(sender='ann@example.com', since='20260901')
    server.search_archive(sender='ann@example.com', since='20260901', by_thread=True)
    assert archive.asked[0][1] == archive.asked[1][1]


def test_the_maintainer_diff_can_be_done_from_two_searches(stub: Install) -> None:
    """What is still waiting for the maintainer, end to end.

    There is no tool for this on purpose. Both halves come back keyed on
    `root', so subtracting one from the other is set arithmetic the
    caller can do -- which is the point: what counts as having answered
    a series is the maintainer's judgement, not this server's.
    """
    archive = stub(
        hits=[
            hit('mine@example.com', subject='Re: [PATCH] usb: fix', refs=('answered@example.com',), day=4),
            hit('theirs@example.com', subject='[PATCH] usb: other', day=5),
        ]
    )
    candidates = server.search_archive(subject='PATCH', by_thread=True)

    archive.hits = (archive.hits[0],)
    spoken = server.search_archive(sender=ME[1], by_thread=True)

    answered = {row['root'] for row in spoken['threads']}
    waiting = [row for row in candidates['threads'] if row['root'] not in answered]
    assert [row['root'] for row in waiting] == ['theirs@example.com']


# --- whoami ------------------------------------------------------------


def test_whoami_reports_the_identity_and_the_window(identity: Install, stub: Install) -> None:
    stub()
    identity({'name': ME[0], 'address': ME[1]})
    found = server.whoami()
    assert found['identity_known'] is True
    assert found['identity']['address'] == ME[1]
    assert found['coverage']['inboxes'] == ['usb', 'staging']


def test_whoami_admits_when_nobody_is_configured(identity: Install, stub: Install) -> None:
    """Setup skips this question happily, so it has to be an ordinary answer."""
    stub()
    identity(None)
    assert server.whoami()['identity_known'] is False


def test_a_damaged_identity_file_is_not_a_crash(
    identity: Install, stub: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub()
    path = identity({})
    path.write_text('{not json', encoding='utf-8')
    assert server.whoami()['identity_known'] is False


# --- narrowing to a subsystem ------------------------------------------


def test_a_subsystem_is_searched_in_its_own_archives(stub: Install, tracked: Path) -> None:
    """The whole point: a smaller corpus, not a filter over the big one."""
    archive = stub()

    server.search_archive(subsystem=['PAGE CACHE'], subject='usb')

    assert archive.searched[0] == (tracked / 'page_cache-patches', tracked / 'page_cache-mailinglist')


def test_one_subsystem_need_not_be_wrapped_in_a_list(stub: Install, tracked: Path) -> None:
    """An agent sends whichever it thinks of; both are the same request."""
    archive = stub()

    server.search_archive(subsystem='PAGE CACHE', subject='usb')

    assert archive.searched[0] == (tracked / 'page_cache-patches', tracked / 'page_cache-mailinglist')


def test_a_kind_picks_which_of_the_two_archives_to_ask(stub: Install, tracked: Path) -> None:
    """Bug reports carry no diff, so the mailinglist archive is the only place they are."""
    archive = stub()

    server.search_archive(subsystem=['PAGE CACHE'], kind='mailinglist', since='20260917')

    assert archive.searched[0] == (tracked / 'page_cache-mailinglist',)


def test_several_subsystems_are_asked_in_one_search(stub: Install, tracked: Path) -> None:
    archive = stub()

    server.search_archive(subsystem=['PAGE CACHE', 'XARRAY'], kind='patches', since='20260917')

    assert archive.searched[0] == (tracked / 'page_cache-patches', tracked / 'xarray-patches')


def test_naming_a_subsystem_is_itself_a_complete_search(stub: Install, tracked: Path) -> None:
    """It said what to search. Demanding another filter would be pedantry."""
    archive = stub()

    found = server.search_archive(subsystem=['PAGE CACHE'], kind='patches')

    assert 'error' not in found
    assert archive.asked[0][1] == [ALL_DATES]


def test_an_untracked_subsystem_is_refused(stub: Install, tracked: Path) -> None:
    """Silently searching nothing would come back looking like good news."""
    stub()

    found = server.search_archive(subsystem=['NFSD'], subject='usb')

    assert 'NFSD' in found['error']


def test_being_refused_says_which_subsystems_there_are(stub: Install, tracked: Path) -> None:
    stub()

    found = server.search_archive(subsystem=['NFSD'], subject='usb')

    assert 'PAGE CACHE' in found['error'] and 'XARRAY' in found['error']


def test_a_kind_with_no_subsystem_is_a_question_back(stub: Install, tracked: Path) -> None:
    """kind picks between one subsystem's archives; alone it means nothing."""
    stub()

    found = server.search_archive(kind='patches', subject='usb')

    assert 'error' in found


def test_an_unnarrowed_search_still_asks_the_whole_archive(stub: Install, tracked: Path) -> None:
    """Narrowing is an option, not a new requirement."""
    archive = stub()

    found = server.search_archive(subject='usb')

    assert archive.searched[0] == ()
    assert 'scope' not in found


# --- saying what was actually searched ---------------------------------


def test_a_narrowed_answer_names_the_archives_it_asked(stub: Install, tracked: Path) -> None:
    stub()

    found = server.search_archive(subsystem=['PAGE CACHE'], kind='patches', since='20260917')

    assert [archive['archive'] for archive in found['scope']['archives']] == ['page_cache-patches']


def test_a_narrowed_answer_carries_the_caveat_about_shared_lists(stub: Install, tracked: Path) -> None:
    """PAGE CACHE and XARRAY have the same L: lines, so the same mailinglist archive."""
    stub()

    found = server.search_archive(subsystem=['PAGE CACHE'], kind='mailinglist', since='20260917')

    assert 'list-scoped' in found['scope']['note']


def test_a_narrowed_answer_names_the_subsystem_in_the_maintainers_spelling(stub: Install, tracked: Path) -> None:
    stub()

    found = server.search_archive(subsystem=['page_cache'], kind='patches', since='20260917')

    assert found['scope']['subsystems'] == ['PAGE CACHE']


def test_thread_mode_narrows_the_same_way(stub: Install, tracked: Path) -> None:
    """The two modes are one question; scoping cannot belong to only one."""
    archive = stub(hits=[hit('one@example.com')])

    found = server.search_archive(subsystem=['PAGE CACHE'], kind='patches', by_thread=True, since='20260917')

    assert archive.searched[0] == (tracked / 'page_cache-patches',)
    assert found['scope']['kinds'] == ['patches']


# --- the listing -------------------------------------------------------


def test_the_listing_names_every_tracked_subsystem(tracked: Path) -> None:
    found = server.list_subsystems()

    assert [entry['name'] for entry in found['subsystems']] == ['PAGE CACHE', 'XARRAY']


def test_the_listing_says_which_archives_each_subsystem_has(tracked: Path) -> None:
    found = server.list_subsystems()

    assert found['subsystems'][0]['kinds'] == ['patches', 'mailinglist']


def test_the_listing_explains_what_each_kind_can_answer(tracked: Path) -> None:
    """A caller picking a kind is picking what it is able to find at all."""
    assert 'bug report carries no diff' in server.list_subsystems()['note']
