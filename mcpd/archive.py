# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
"""Read-only retrieval over the container's local mail archive.

Everything the MCP server answers comes from here, and this is the only
place that assembles a lei query.  That is deliberate: lei's argv handling
has a trap in it (see `build_terms') that is invisible when you get it
wrong -- no error, no warning, just zero results -- so there is one place
to get it right rather than one per tool.

The archive is public-inbox's extindex, which spans every subsystem the
maintainer tracks, registered as a lei external by the setup app.  lei is
the retrieval layer; nothing here parses Xapian, scrapes our own /lore/,
or shells out to b4.  What comes back is handed to liblore.series, which
turns messages into summaries.  Coverage is reported alongside every
result, because this archive is a time-windowed slice of some subsystems
and an empty answer means "not here", not "never happened".

This module holds no state that outlives a call except a short coverage
cache, and runs no command that writes: every lei invocation is a query.
"""

import datetime
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from liblore.series import SCHEMA_VERSION, ThreadSummary, summarize_thread, summarize_threads
from liblore.utils import split_mbox

logger = logging.getLogger('maintainer-mcp.archive')

LEICMD = 'lei'

# Generous, because the first query of a container's life pays for starting
# lei-daemon; a warm header-only query over the whole archive is a fraction
# of a second.  A timeout is not optional -- this runs behind a request that
# something else is waiting on.
DEFAULT_TIMEOUT = 120

# What a tool hands back when the caller does not say.  A busy list buries
# an agent, and an answer it cannot read is not an answer.  Callers that
# want more ask for more, and always learn when they were cut off.
DEFAULT_LIMIT = 25

# The biggest single page we will hand back. This is our ceiling, not
# lei's: lei has no result cap. The 10000 in LeiQuery.pm is the size of one
# Xapian batch (`$tot > 10000 ? 10000 : $tot' sets `mset_opt->{limit}',
# while `mset_opt->{total}' takes the caller's --limit whole), and
# LeiXSearch.pm `_mset_more' walks batch after batch until it has `total'.
# So `-n 250000' is answered, not clamped. The reason to cap is that an
# agent has to read what comes back; see DEFAULT_LIMIT.
MAX_LIMIT = 500

# How far a counting walk will go before it gives up and says so. Counting
# is header-only and cheap -- the whole live archive comes back in about
# two and a half seconds -- but "cheap" is not "free", and a tool that
# answers a request must have a bound it can name.
SCAN_CAP = 20000

# lei's `--offset' cannot be used, so paging is done here: ask for
# offset+limit+1 rows and slice. The flag is real in the option spec
# (LEI.pm `offset=i') and documented in lei-q(1) ("Shift start of search
# results"), but LeiQuery.pm line 53 reads
#
#     $self->{mset_opt}->{offset} //= 0;
#
# and nothing ever copies $opt->{offset} into mset_opt, so the value is
# parsed and dropped. Verified against the live archive: --offset 0 and
# --offset 3 return the same rows. Re-fetching the skipped prefix is the
# cost of that; for header-only rows over a local Xapian it is small, and
# it is honest, which a silently ignored flag is not.

# How many threads to expand in one mboxrd query. Bigger is fewer round
# trips, but `-t' pulls in every message of every thread named, and one
# stable-review posting can be a thousand patches on its own -- so this is
# small enough that a bad draw is one slow query rather than a timeout.
SUMMARIZE_CHUNK = 25

# Xapian range covering every plausible message date, for the "what is in
# here at all?" queries behind coverage().  public-inbox has no match-all
# term, and a query with no terms is an error.
ALL_DATES = 'd:19700101..'

# Boolean operators belong between argv items, never inside one -- see
# build_terms().
_OPERATORS = (' AND ', ' OR ', ' NOT ', ' XOR ')

# ...and a term that *is* one is the join between its neighbours, which is
# the one thing the guard has to let past.
_BARE_OPERATORS = frozenset(op.strip() for op in _OPERATORS)


class ArchiveError(RuntimeError):
    """A lei query could not be run, or did not come back usable."""


def _parse_when(value: Optional[str]) -> Optional[datetime.datetime]:
    """Turn one of lei's ISO-8601 stamps into a datetime, or None.

    lei writes UTC as a trailing `Z', which datetime.fromisoformat only
    learned to read in 3.11; the container is well past that, but a bad
    stamp still should not take a whole query down with it.
    """
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        logger.warning('Unparsable date from lei: %r', value)
        return None


def _first_addr(pairs: Any) -> Tuple[str, str]:
    """Read lei's `f' field -- a list of [name, address] pairs -- as one addr.

    Either half can be null (a From: with no display name is ordinary), and
    a malformed record should cost one field rather than the whole result,
    so anything unexpected reads as the empty pair.
    """
    if not isinstance(pairs, list) or not pairs:
        return ('', '')
    first = pairs[0]
    if not isinstance(first, list) or len(first) != 2:
        return ('', '')
    name, addr = first
    # Both halves come out of lei's JSON, so neither is a str until we say so.
    return (str(name or ''), str(addr or ''))


@dataclass(frozen=True)
class Hit:
    """One message, as the header-only search sees it.

    No body.  A hit is enough to decide whether a thread is worth opening,
    and opening it is a separate, deliberate call -- which is both the
    volume fix and the injection posture: list-authored prose reaches the
    agent because somebody asked for that thread, not as the default shape
    of every answer.
    """

    msgid: str
    subject: str
    author: Tuple[str, str]
    date: Optional[datetime.datetime]
    received: Optional[datetime.datetime]
    refs: Tuple[str, ...] = ()
    relevance: Optional[int] = None

    @classmethod
    def from_json(cls, record: Dict[str, Any]) -> 'Hit':
        """Build a Hit from one line of `lei q -f jsonl'.

        The short key names are lei's own (LeiOverview.pm `_unbless_smsg'):
        `m' message-id, `s' subject, `f' from, `dt' the Date: header, `rt'
        when the archive received it, `pct' relevance.
        """
        refs = record.get('refs')
        return cls(
            msgid=record.get('m') or '',
            subject=record.get('s') or '',
            author=_first_addr(record.get('f')),
            date=_parse_when(record.get('dt')),
            received=_parse_when(record.get('rt')),
            refs=tuple(refs) if isinstance(refs, list) else (),
            relevance=record.get('pct'),
        )

    @property
    def root(self) -> str:
        """The Message-ID this hit's thread hangs off, as best we can tell.

        `refs' is the References: header in order, so its first entry is
        the oldest ancestor -- the thread root. A message with no
        References: started its own thread, so it is its own root.

        This is a grouping key, not a promise that the archive holds that
        message: a reply mirrored inside the window can easily name a root
        that predates it. Anything that needs to *fetch* the thread should
        ask by a Message-ID it actually saw (see ThreadGroup.anchor).
        """
        return self.refs[0] if self.refs else self.msgid

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable view of this hit."""
        return {
            'msgid': self.msgid,
            'subject': self.subject,
            'author': {'name': self.author[0], 'email': self.author[1]},
            'date': self.date.isoformat() if self.date else None,
            'received': self.received.isoformat() if self.received else None,
            'refs': list(self.refs),
            'relevance': self.relevance,
        }


@dataclass(frozen=True)
class Coverage:
    """What was searched, and over what span of time.

    The whole point of the container is that it mirrors *some* subsystems
    over *some* window, so "no results" is ambiguous in a way it never is
    on lore.kernel.org.  Every answer carries this, so a caller can tell
    "it did not happen" from "it is not mirrored here" without being told
    separately, and can say so instead of guessing.
    """

    external: str
    inboxes: Tuple[str, ...] = ()
    earliest: Optional[datetime.datetime] = None
    latest: Optional[datetime.datetime] = None
    ready: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable view, with the caveat spelled out."""
        if self.ready:
            note = (
                'This archive holds only the subsystems this maintainer tracks, '
                'and only messages received in the window above. An empty result '
                'means the local archive has nothing matching -- not that no such '
                'message exists.'
            )
        else:
            note = (
                'This archive has not been built yet: no subsystems are being '
                'tracked, or mail has been pulled but not yet indexed. Every '
                'search will come back empty until then, which says nothing at '
                'all about what exists. Tell the maintainer to set the container '
                'up at its dashboard rather than reporting an empty result.'
            )
        return {
            'searched': self.external,
            'ready': self.ready,
            'inboxes': list(self.inboxes),
            'earliest': self.earliest.isoformat() if self.earliest else None,
            'latest': self.latest.isoformat() if self.latest else None,
            'note': note,
        }


@dataclass(frozen=True)
class Page:
    """A bounded set of hits that says what it left out.

    A cap that truncates silently is worse than no cap: it turns a partial
    answer into a confident-looking whole one.  `truncated' is the honest
    half of the bargain and is set from asking lei for one more row than
    the caller wanted.
    """

    hits: Tuple[Hit, ...]
    limit: int
    truncated: bool
    coverage: Coverage
    terms: Tuple[str, ...] = ()
    offset: int = 0

    @property
    def next_offset(self) -> Optional[int]:
        """Where to start to get the next page, or None at the end."""
        return self.offset + len(self.hits) if self.truncated else None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable view of this page of results."""
        return {
            'schema': SCHEMA_VERSION,
            'query': list(self.terms),
            'count': len(self.hits),
            'limit': self.limit,
            'offset': self.offset,
            'truncated': self.truncated,
            'next_offset': self.next_offset,
            'hits': [hit.to_dict() for hit in self.hits],
            'coverage': self.coverage.to_dict(),
        }


@dataclass(frozen=True)
class ThreadGroup:
    """The messages of one thread that a single search matched.

    Grouping is done on `Hit.root', which comes from the References:
    header, so it needs no extra query and no walk over In-Reply-To. What
    it gives is a thread *key* and the shape of the thread as this search
    saw it -- enough to count, sort and page threads before deciding which
    ones are worth opening.

    It is deliberately not a ThreadSummary: no bodies have been read, so
    there are no trailers here and nothing has been told apart as a patch.
    Archive.summarize() is the step that does that, and it runs over a
    page, not over everything counted.
    """

    root: str
    hits: Tuple[Hit, ...]

    @property
    def anchor(self) -> str:
        """A Message-ID from this thread that the archive definitely holds.

        Asking for the thread by `root' would be wrong: a reply can name a
        root that was never mirrored here, and `m:<root>' would then find
        nothing at all. Every hit, by definition, came out of the archive.
        """
        return self.hits[0].msgid

    @property
    def first(self) -> Optional[datetime.datetime]:
        """When the earliest matched message arrived."""
        return self.hits[0].received

    @property
    def last(self) -> Optional[datetime.datetime]:
        """When the most recent matched message arrived."""
        return self.hits[-1].received

    @property
    def subject(self) -> str:
        """The earliest matched message's subject."""
        return self.hits[0].subject

    @property
    def authors(self) -> List[Tuple[str, str]]:
        """Everyone who wrote one of the matched messages, oldest first.

        Only the messages this search matched, not the whole thread --
        naming it `authors' rather than `participants' is the difference,
        and the same one that separates a ThreadGroup from a
        ThreadSummary.
        """
        seen: Dict[Tuple[str, str], None] = {}
        for hit in self.hits:
            seen.setdefault(hit.author, None)
        return list(seen)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable view of this thread.

        `root' is the thread key: it is what two searches can be compared
        on, which is how a caller works out set differences like "patch
        threads I have not replied in" without this server deciding what
        counts as a reply. `msgid' is the anchor -- ask for the thread
        with that, never with `root', which may name a message this
        archive never mirrored.
        """
        return {
            'root': self.root,
            'msgid': self.anchor,
            'subject': self.subject,
            'first': self.first.isoformat() if self.first else None,
            'last': self.last.isoformat() if self.last else None,
            'count': len(self.hits),
            'authors': [{'name': name, 'email': email} for name, email in self.authors],
        }


def group_threads(hits: Sequence[Hit]) -> List[ThreadGroup]:
    """Fold hits into threads, most recently active first.

    Within a group the hits are oldest first, so `first'/`last' and
    `anchor' all mean what they say regardless of what order lei returned.
    """
    by_root: Dict[str, List[Hit]] = {}
    for hit in hits:
        by_root.setdefault(hit.root, []).append(hit)

    groups = [ThreadGroup(root=root, hits=tuple(sorted(found, key=_received_key))) for root, found in by_root.items()]
    groups.sort(key=lambda group: _received_key(group.hits[-1]), reverse=True)
    return groups


def _received_key(hit: Hit) -> datetime.datetime:
    """Sort key over received time that tolerates a missing stamp."""
    return hit.received or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


@dataclass(frozen=True)
class Scan:
    """Every message matching a query -- or as many as the budget allowed.

    A Page answers "here are some"; a Scan answers "here are all of them,
    and here is how I know". That distinction is the whole reason this
    exists: a count taken out of a capped page is not a count, and a tool
    that reports one as if it were is telling the maintainer something
    false about their own inbox.
    """

    hits: Tuple[Hit, ...]
    cap: int
    complete: bool
    coverage: Coverage
    terms: Tuple[str, ...] = ()

    def groups(self) -> List[ThreadGroup]:
        """Fold this scan's hits into threads, most recently active first."""
        return group_threads(self.hits)

    def to_dict(self) -> Dict[str, Any]:
        """Describe what was walked, without the hits themselves."""
        return {
            'query': list(self.terms),
            'messages': len(self.hits),
            'cap': self.cap,
            'complete': self.complete,
            'note': (
                'Every matching message was walked; the totals below are real.'
                if self.complete
                else f'Stopped after {self.cap} messages. Totals below are counted '
                'over that much of the archive and are lower bounds, not totals.'
            ),
        }


@dataclass(frozen=True)
class ThreadPage:
    """One page of threads, over a count of threads that is real.

    The message-level Page and this are not the same shape with a
    different row in it.  A page of messages can be sliced before
    anything is counted; a page of *threads* cannot, because which thread
    a message belongs to is only known once every matching message has
    been seen.  So this is always built on a full Scan and then sliced,
    which is what makes `total' the number of threads that matched rather
    than the number of threads visible in one page of messages.

    `scan' carries the other half of that honesty: when the walk hit its
    cap, `total' is a lower bound and some threads came back with only
    part of their matched messages.
    """

    groups: Tuple[ThreadGroup, ...]
    limit: int
    offset: int
    total: int
    scan: Scan

    @property
    def next_offset(self) -> Optional[int]:
        """Where to start to get the next page, or None at the end."""
        nxt = self.offset + len(self.groups)
        return nxt if nxt < self.total else None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable view of this page of threads."""
        return {
            'schema': SCHEMA_VERSION,
            'query': list(self.scan.terms),
            'total': self.total,
            'count': len(self.groups),
            'limit': self.limit,
            'offset': self.offset,
            'next_offset': self.next_offset,
            'threads': [group.to_dict() for group in self.groups],
            'scan': self.scan.to_dict(),
            'coverage': self.scan.coverage.to_dict(),
        }


# YYYYMMDD with or without the dashes; the dashes are what we emit.
_DATE_RE = re.compile(r'^(\d{4})-?(\d{2})-?(\d{2})$')


def _lei_date(value: str, name: str, *, end_of_day: bool = False) -> str:
    """Normalise a date for a lei `rt:' range, or refuse it.

    Two separate traps live here, and both are silent.

    The first is that `rt:' is a numeric range over Unix timestamps, so a
    bare `20260917' is a perfectly good number and is taken as one:
    20,260,917 seconds is 1970-08-23.  As a lower bound that sits before
    every message and matches the whole archive, which looks like a
    working filter; as an upper bound it matches nothing at all.

    The second is that a date is not a moment, and lei resolves one with
    git's approxidate, where the *current* date means now rather than
    this morning.  `rt:2026-09-17..' asked on the evening of the 17th
    behaves exactly like `rt:today..' and answers nothing, so "what came
    in today" is the one question it reliably gets wrong.

    So the time is always spelled out: a lower bound starts at midnight,
    an upper bound ends at 23:59:59, and both ends of a range name whole
    days inclusively, the way somebody asking for dates means them.

    Raises:
        ValueError: if *value* is not a date, rather than letting it
            through to become a silent timestamp.
    """
    match = _DATE_RE.match(value.strip())
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            day_of = datetime.date(year, month, day).isoformat()
            return f'{day_of}T23:59:59Z' if end_of_day else f'{day_of}T00:00:00Z'
        except ValueError:
            pass
    raise ValueError(
        f'{name}={value!r} is not a date. Give YYYYMMDD or YYYY-MM-DD; anything '
        'else would be read as a Unix timestamp and quietly match the wrong mail.'
    )


def build_terms(
    text: Optional[str] = None,
    sender: Optional[str] = None,
    subject: Optional[str] = None,
    nonquoted: Optional[str] = None,
    to: Optional[str] = None,
    cc: Optional[str] = None,
    participant: Optional[str] = None,
    list_id: Optional[str] = None,
    filename: Optional[str] = None,
    subject_or_body: Optional[str] = None,
    diff_path: Optional[str] = None,
    diff_added: Optional[str] = None,
    diff_removed: Optional[str] = None,
    diff_hunk: Optional[str] = None,
    patch_id: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    extra: Sequence[str] = (),
) -> List[str]:
    """Assemble lei search terms, one per argv item.

    Every term is its own argument, and that is the whole point.  lei
    treats a single argv containing spaces as a phrase, so

        subprocess.run(['lei', 'q', 'f:x AND s:y'])

    searches for the literal string "f:x AND s:y", matches nothing, exits
    0 and prints no warning.  Terms passed separately are ANDed by lei
    itself, which is what a caller means every time.

    *text* searches everywhere at once -- Xapian's default prefix covers
    Message-ID, subject, From, quoted and non-quoted body, and attachment
    filenames.  The named filters below narrow to one of those instead.

    *sender*, *to* and *cc* each match one header; *participant* matches
    To, Cc and From together, which is how to ask whether somebody was
    involved at all without caring in what capacity.

    *nonquoted* searches body text that is not a quote of an earlier
    message (`nq:'), which is how a real trailer is told from the same
    string quoted back in a reply.  It is a filter, not an answer --
    measured at about 93% precision on the live archive -- so what it
    narrows still has to be read properly by liblore.series.
    *subject_or_body* (`bs:') is the opposite move, widening to both.

    The *diff_* filters search inside the patch itself: the paths it
    touches, its added (`+') and removed (`-') lines, and the hunk header
    that usually names the enclosing function.  *patch_id* matches
    `git patch-id --stable' output, so a patch can be found again after
    it was rebased and its Message-ID changed.

    *since* and *until* bound when the archive received a message.  They
    are accepted as `YYYYMMDD' or `YYYY-MM-DD' and name whole days
    inclusively: *since* starts at midnight and *until* ends at 23:59:59.
    The time is spelled out rather than left to lei for the two reasons
    :func:`_lei_date` explains at length, both of which fail quietly.

    *extra* passes raw search terms through untouched, for the prefixes
    with no parameter of their own (`tc:', `dfctx:', `dfpre:', `dfpost:',
    `dfblob:') and for negation.  Xapian is loaded with FLAG_LOVEHATE, so
    a leading `-' excludes -- `-f:syzbot@syzkaller.appspotmail.com' is one
    space-free token that survives the round trip.  Negation only ever
    narrows something else, though: public-inbox deliberately leaves
    FLAG_PURE_NOT off as a denial-of-service vector, so a query made of
    nothing but exclusions matches nothing.

    Negation is spelled out by the caller here, and is deliberately not
    offered as a leading `-' on the named filters.  Kernel subjects really
    do start with a dash -- `-Wmaybe-uninitialized' is a subject line, not
    an exclusion -- and a filter that silently inverted one would be worse
    than no negation at all.

    Raises:
        ValueError: if a term carries a boolean operator inside it, which
            is the failure above written by hand, or if *since* or
            *until* is not a date.
    """
    terms: List[str] = []
    if text:
        terms.append(text)
    # The prefixes are PublicInbox::Search's own (%prob_prefix and
    # %PATCH_PROB_COMMON); the parameter names are what a caller would
    # think to ask for, since an agent picking from a tool schema should
    # not have to know that the plus lines of a diff are called `dfb'.
    for value, prefix in (
        (sender, 'f'),
        (subject, 's'),
        (nonquoted, 'nq'),
        (to, 't'),
        (cc, 'c'),
        (participant, 'a'),
        (list_id, 'l'),
        (filename, 'n'),
        (subject_or_body, 'bs'),
        (diff_path, 'dfn'),
        (diff_added, 'dfb'),
        (diff_removed, 'dfa'),
        (diff_hunk, 'dfhh'),
        (patch_id, 'patchid'),
    ):
        if value:
            terms.append(f'{prefix}:{value}')
    if since or until:
        low = _lei_date(since, 'since') if since else ''
        high = _lei_date(until, 'until', end_of_day=True) if until else ''
        terms.append(f'rt:{low}..{high}')
    terms.extend(extra)

    for term in terms:
        # A term that is nothing but an operator joins the two around it,
        # and that is how an OR gets spelled here. lei hands every argv to
        # Xapian's parser, so ['dfn:mm/filemap.c', 'OR', 'dfn:mm/truncate.c']
        # is a real union of 31 messages, while the same thing in one argv
        # is a phrase nobody ever wrote and matches none.
        if term.strip() in _BARE_OPERATORS:
            continue
        if any(op in f' {term} ' for op in _OPERATORS):
            raise ValueError(
                f'{term!r} puts a boolean operator inside one term; lei reads that '
                'as a literal phrase and matches nothing. Pass each term separately.'
            )
    return terms


@dataclass
class Archive:
    """The local extindex, queried through lei.

    *extindex* is the path the setup app registered as a lei external; it
    is passed to every query with `-O', which has two effects worth being
    explicit about.  It restricts the search to that one external, and --
    per LeiQuery.pm, `--local is enabled by default unless --only is used'
    -- it also leaves lei's own store out, so a query answers from the
    container's archive and nothing else.

    *config* is public-inbox's generated config, read only to name the
    inboxes the extindex spans for coverage reporting.

    The extindex is the default corpus, not the only one. Every query
    method takes *externals*, and korgalore's per-subsystem archives are
    v2 public-inbox archives with their own Xapian shards, so passing one
    asks the same question of a far smaller corpus -- which is what
    narrowing to a subsystem is (see subsystems.py). This class stays
    unaware of what any of those archives mean; it is handed paths.
    """

    extindex: Path
    config: Optional[Path] = None
    timeout: int = DEFAULT_TIMEOUT
    coverage_ttl: int = 300
    _coverage: Dict[Tuple[str, ...], Tuple[float, Coverage]] = field(default_factory=dict, repr=False)

    def _externals(self, externals: Sequence[Path] = ()) -> Tuple[Path, ...]:
        """The archives a query should run against: the ones asked for, or the extindex."""
        return tuple(externals) if externals else (self.extindex,)

    def _run(self, opts: Sequence[str], terms: Sequence[str], externals: Sequence[Path] = ()) -> bytes:
        """Run one lei query and return its stdout.

        *opts* are lei's own switches and *terms* are the search itself,
        and they are kept apart so a `--' can go between them.  Without
        it, any term Xapian expects to start with `-' is eaten by lei's
        own option parser first: an exclusion like

            -f:gregkh@linuxfoundation.org

        is read as `--format=:gregkh@linuxfoundation.org' and the query
        dies with "bad mail --format", while `-s:usb' becomes an
        unrecognised `--sort'.  `--' costs nothing when no term starts
        with a dash, so it is unconditional rather than something the
        caller has to remember.

        Raises:
            ArchiveError: if lei is missing, times out, or fails.
        """
        picked: List[str] = []
        for external in self._externals(externals):
            picked += ['-O', str(external)]
        cmd = [LEICMD, 'q', '--no-save', '--no-remote', *picked, *opts, '--', *terms]
        logger.debug('Running: %s', ' '.join(cmd))
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=self.timeout)
        except FileNotFoundError as e:
            raise ArchiveError(f"lei command '{LEICMD}' not found. Is it installed?") from e
        except subprocess.TimeoutExpired as e:
            raise ArchiveError(f'lei query timed out after {self.timeout}s') from e

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).decode(errors='replace').strip()
            raise ArchiveError(f'lei query failed (exit {proc.returncode}): {detail}')
        return proc.stdout

    def _jsonl(
        self,
        terms: Sequence[str],
        limit: int,
        reverse: bool = False,
        externals: Sequence[Path] = (),
    ) -> List[Hit]:
        """Run a header-only query and parse what comes back.

        `--no-save' is not merely tidiness: an ordinary `lei q' can record
        a saved search for `lei up' to refresh later, and this container
        has a loop whose whole job is refreshing the saved searches that
        korgalore created.  A question asked by an agent must not quietly
        become something the container then syncs forever.
        """
        opts = ['-f', 'jsonl', '-n', str(limit)]
        if reverse:
            opts.insert(0, '-r')
        hits: List[Hit] = []
        for line in self._run(opts, terms, externals).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning('Skipping unparsable line from lei: %r', line[:200])
                continue
            # A `null' terminator shows up in some output formats; a record
            # without a message-id is nothing we can act on either way.
            if not isinstance(record, dict):
                continue
            hits.append(Hit.from_json(record))
        return hits

    def ready(self, externals: Sequence[Path] = ()) -> bool:
        """Is there an archive to search at all?

        False on a container nobody has finished setting up: the extindex
        is built by the mail loop after the first subsystem is tracked, so
        until then the path simply is not there. The same is true one
        subsystem at a time -- korgalore creates a per-subsystem archive
        when the first message for it arrives, not when it is tracked --
        so this answers for whichever archives a query names.

        This is checked rather than left to lei because lei does not treat
        it as an empty archive -- `-O' on a path that does not exist warns
        "gone, perhaps run: lei forget-external", finds nothing to search
        and exits 1. Handing that to a caller turns "not set up yet" into
        what looks like a broken installation, and the suggested fix is
        wrong here: the external is not stale, it is not built yet.
        """
        return all(external.exists() for external in self._externals(externals))

    def _nothing(
        self,
        terms: Sequence[str],
        limit: int,
        offset: int = 0,
        externals: Sequence[Path] = (),
    ) -> Page:
        """An empty page, for when there is no archive to ask."""
        return Page(
            hits=(),
            limit=limit,
            truncated=False,
            coverage=self.coverage(externals),
            terms=tuple(terms),
            offset=offset,
        )

    def search(
        self,
        terms: Sequence[str],
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        externals: Sequence[Path] = (),
    ) -> Page:
        """Return one page of header-only hits, starting at *offset*.

        *terms* comes from build_terms(); each item is one lei search term.
        *externals* is which archives to ask, defaulting to the extindex.

        Paging is done here rather than by lei -- see the note on
        MAX_LIMIT about `--offset' being parsed and dropped -- so this asks
        for offset+limit+1 rows and slices. The +1 is what makes
        `truncated' something observed rather than guessed from a page
        that happened to come back full.

        Raises:
            ValueError: if *limit* or *offset* is not a usable size.
            ArchiveError: if the query could not be run.
        """
        if limit < 1:
            raise ValueError(f'limit must be at least 1, not {limit}')
        if limit > MAX_LIMIT:
            raise ValueError(f'limit must be at most {MAX_LIMIT}, not {limit}')
        if offset < 0:
            raise ValueError(f'offset must not be negative, not {offset}')
        if offset + limit > SCAN_CAP:
            raise ValueError(f'offset + limit must be at most {SCAN_CAP}, not {offset + limit}')
        if not terms:
            raise ValueError('a search needs at least one term')
        if not self.ready(externals):
            return self._nothing(terms, limit, offset, externals)

        # lei documents its limit as fuzzy, because it can apply per
        # external rather than to the whole answer. Asking for one row
        # more than the page and slicing is what makes that not matter:
        # over-delivery is absorbed by the slice, and `truncated' is
        # observed either way. Measured over two of korgalore's archives,
        # lei merges them into one run ordered newest first with no
        # duplicates, so the slice is a page of the whole answer and not
        # of one archive's share of it.
        found = self._jsonl(terms, offset + limit + 1, externals=externals)
        window = found[offset : offset + limit]
        return Page(
            hits=tuple(window),
            limit=limit,
            truncated=len(found) > offset + limit,
            coverage=self.coverage(externals),
            terms=tuple(terms),
            offset=offset,
        )

    def scan(self, terms: Sequence[str], cap: int = SCAN_CAP, externals: Sequence[Path] = ()) -> Scan:
        """Walk *every* message matching *terms*, up to *cap*.

        This is the counting primitive. It exists because a page cannot
        answer "how many?", and questions like "what is still waiting on
        me?" are counting questions before they are listing ones.

        One query does it: lei's --limit is a total, not a batch size, and
        LeiXSearch.pm `_mset_more' walks Xapian 10000 rows at a time until
        it has that total. Header-only rows over a local extindex are
        cheap enough that walking the whole archive is measured in
        seconds, which is why this is affordable at all.

        Asking for cap+1 is the same trick as search(): `complete' then
        says whether the cap was actually reached, rather than leaving a
        caller to wonder about an answer of exactly *cap*.

        Raises:
            ValueError: if *cap* is not a usable size, or *terms* is empty.
            ArchiveError: if the query could not be run.
        """
        if cap < 1:
            raise ValueError(f'cap must be at least 1, not {cap}')
        if cap > SCAN_CAP:
            raise ValueError(f'cap must be at most {SCAN_CAP}, not {cap}')
        if not terms:
            raise ValueError('a scan needs at least one term')
        if not self.ready(externals):
            return Scan(hits=(), cap=cap, complete=True, coverage=self.coverage(externals), terms=tuple(terms))

        found = self._jsonl(terms, cap + 1, externals=externals)
        return Scan(
            hits=tuple(found[:cap]),
            cap=cap,
            complete=len(found) <= cap,
            coverage=self.coverage(externals),
            terms=tuple(terms),
        )

    def search_threads(
        self,
        terms: Sequence[str],
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        cap: int = SCAN_CAP,
        externals: Sequence[Path] = (),
    ) -> ThreadPage:
        """Return one page of *threads* matching *terms*, newest activity first.

        The same query as search(), answered one row per thread instead of
        one row per message.  On a mailing list that is a large
        difference and not a cosmetic one: a fortnight of one subsystem is
        a few thousand messages and a few hundred threads, and a caller
        that wants to reason about threads would otherwise have to page
        through every message to find out which threads there were.

        It costs a full scan rather than a page, because a thread cannot
        be counted from a slice -- see ThreadPage.  That is affordable
        because the scan is header-only; it is not free, so it is bounded
        by *cap* and says when it hit it.

        Raises:
            ValueError: if *limit*, *offset* or *cap* is not a usable size.
            ArchiveError: if the query could not be run.
        """
        if limit < 1:
            raise ValueError(f'limit must be at least 1, not {limit}')
        if limit > MAX_LIMIT:
            raise ValueError(f'limit must be at most {MAX_LIMIT}, not {limit}')
        if offset < 0:
            raise ValueError(f'offset must not be negative, not {offset}')

        found = self.scan(terms, cap=cap, externals=externals)
        groups = found.groups()
        return ThreadPage(
            groups=tuple(groups[offset : offset + limit]),
            limit=limit,
            offset=offset,
            total=len(groups),
            scan=found,
        )

    def summarize(self, msgids: Sequence[str], chunk: int = SUMMARIZE_CHUNK) -> List[ThreadSummary]:
        """Summarise the threads containing *msgids*, in as few queries as possible.

        This is the "expand" half of narrow-then-expand: the caller has
        already decided, from headers alone, which handful of threads are
        worth reading bodies for, and this reads those and no others.

        Several threads come back per query because a bare `OR' can be
        passed as its own argv item. That is not a detail to be clever
        about -- it is the *only* way to pass a boolean to lei.
        Search.pm's query_argv_to_string() quotes any argv item containing
        whitespace, so

            lei q 'm:a OR m:b'          # searches for a literal phrase

        matches nothing, while

            lei q 'm:a' OR 'm:b'        # three argv items, joined by spaces

        is the query one meant. Verified both ways against the live
        archive.

        Raises:
            ArchiveError: if a query could not be run.
        """
        wanted = [m.strip().lstrip('<').rstrip('>') for m in msgids]
        wanted = [m for m in wanted if m]
        if not wanted or not self.ready():
            return []

        summaries: List[ThreadSummary] = []
        for start in range(0, len(wanted), max(1, chunk)):
            batch = wanted[start : start + max(1, chunk)]
            terms: List[str] = []
            for msgid in batch:
                if terms:
                    terms.append('OR')
                terms.append(f'm:{msgid}')
            raw = self._run(['-f', 'mboxrd', '-t', '-n', str(len(batch))], terms)
            if raw.strip():
                summaries.extend(summarize_threads(split_mbox(raw)))
        return summaries

    def thread(self, msgid: str) -> Optional[ThreadSummary]:
        """Summarise the whole thread a Message-ID belongs to.

        `-t' expands to the thread server-side, so this is one command
        rather than a walk over In-Reply-To headers, and the summary comes
        back as structured records rather than raw bodies -- 46x less text
        for the same answer, measured, and the posture that keeps most of
        the internet's prose out of an agent's context.

        Returns None when the archive does not hold that message, which is
        an ordinary answer for a partial mirror, not an error.
        """
        msgid = msgid.strip().lstrip('<').rstrip('>')
        if not msgid:
            raise ValueError('a message-id is required')
        if not self.ready():
            return None

        raw = self._run(['-f', 'mboxrd', '-t'], [f'm:{msgid}'])
        if not raw.strip():
            return None
        msgs = split_mbox(raw)
        if not msgs:
            return None
        return summarize_thread(msgs)

    def threads(self, terms: Sequence[str], limit: int = DEFAULT_LIMIT) -> Tuple[List[ThreadSummary], Page]:
        """Summarise whole threads matching *terms*, not just the matches.

        Returns the summaries and the header-only page they came from, so
        a caller has both the answer and what was searched, capped and cut
        to get it.

        Two queries rather than one, on purpose. `-t' pulls in every
        message of a matching thread, and those extra messages do not
        count towards `--limit' -- so the size of what comes back says
        nothing about whether the *match* set was capped. The header-only
        pass answers that honestly; the mboxrd pass, with the same terms
        and the same limit, is what actually gets read.

        Bodies are needed here and there is no way around it: a
        Reviewed-by: lives in the body of a reply, not in a header. They
        are read and dropped -- what leaves this method is summaries.
        """
        page = self.search(terms, limit)
        if not self.ready():
            return ([], page)
        raw = self._run(['-f', 'mboxrd', '-t', '-n', str(limit)], terms)
        if not raw.strip():
            return ([], page)
        return (summarize_threads(split_mbox(raw)), page)

    def inboxes(self) -> Tuple[str, ...]:
        """Name the inboxes the extindex spans, from public-inbox's config.

        Read straight rather than through configparser: public-inbox
        writes git-config syntax, whose section headers carry a quoted
        subsection name (`[publicinbox "usb-patches"]') that configparser
        has no notion of. The inbox name is that subsection.
        """
        if self.config is None:
            return ()
        try:
            text = self.config.read_text(encoding='utf-8', errors='replace')
        except OSError as e:
            logger.warning('Could not read %s: %s', self.config, e)
            return ()

        names: List[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith('[publicinbox "') or not line.endswith('"]'):
                continue
            name = line[len('[publicinbox "') : -len('"]')]
            if name and name not in names:
                names.append(name)
        return tuple(names)

    def coverage(self, externals: Sequence[Path] = ()) -> Coverage:
        """Report what the archive holds, caching it briefly.

        *externals* narrows this the same way it narrows a search, and it
        has to: a per-subsystem archive holds only what korgalore has
        delivered into it since that subsystem was tracked, which is a
        shorter window than the extindex's. Reporting the whole archive's
        dates for a search of one subsystem's would overstate what an
        empty answer rules out.

        Two one-row queries, oldest and newest by received time. Received
        time rather than the Date: header on purpose: what bounds this
        archive is when korgalore pulled a message into it, and a message
        can carry any Date: its sender liked.

        The result is cached for coverage_ttl seconds -- the mail loop
        only runs every ten minutes, so recomputing this for every tool
        call in a conversation buys nothing.
        """
        picked = self._externals(externals)
        key = tuple(str(external) for external in picked)

        now = time.monotonic()
        cached_entry = self._coverage.get(key)
        if cached_entry is not None:
            cached_at, cached = cached_entry
            if now - cached_at < self.coverage_ttl:
                return cached

        earliest = latest = None
        ready = self.ready(externals)
        if ready:
            try:
                oldest = self._jsonl([ALL_DATES], limit=1, reverse=True, externals=externals)
                newest = self._jsonl([ALL_DATES], limit=1, externals=externals)
                earliest = oldest[0].received if oldest else None
                latest = newest[0].received if newest else None
            except ArchiveError as e:
                # An unreported window is a worse answer than a missing
                # one, but it is not worth failing somebody's search over:
                # say what we know and let the caveat in to_dict() carry
                # the rest.
                logger.warning('Could not determine archive coverage: %s', e)

        # The extindex spans public-inbox inboxes and names them out of
        # public-inbox's config; a per-subsystem archive is one corpus and
        # names itself.
        found = Coverage(
            external=', '.join(key),
            inboxes=self.inboxes() if not externals else tuple(external.name for external in picked),
            earliest=earliest,
            latest=latest,
            ready=ready,
        )
        # Only a real answer is worth remembering. "Not built yet" is a
        # state the container grows out of, and caching it would keep
        # saying so for the rest of the TTL after the first index has
        # finished -- on exactly the setup where somebody is watching.
        if ready:
            self._coverage[key] = (now, found)
        return found
