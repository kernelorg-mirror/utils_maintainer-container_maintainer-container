"""The subsystems this container tracks, and the archives korgalore built for them.

korgalore does not deliver a tracked subsystem's mail into one big pile.
`kgl track-subsystem' writes a conf.d/<key>.toml naming feeds and points
each at its own lei output, and those outputs are real v2 public-inbox
archives with their own Xapian shards -- so each one can be searched on
its own with `-O <path>', exactly like the extindex.

That is what makes focus possible. A maintainer rarely works across every
subsystem at once, and the whole archive is the wrong corpus for "what
came in for PAGE CACHE today": a day of the extindex on a live container
is 542 messages, and a day of page_cache-patches is 6. Narrowing costs
nothing at query time, because korgalore already did the narrowing when
it delivered the mail.

The two kinds are different instruments, and the difference is the whole
reason both exist:

  patches      built from the subsystem's F: file globs, so it really is
               subsystem-scoped -- but it holds patches. A bug report
               carries no diff and so never lands in it.

  mailinglist  built from the subsystem's L: lines, so it holds whole
               mailing lists. That is how bug reports, syzbot mail and
               build regressions are found at all, and it is *list*-scoped
               rather than subsystem-scoped: MAINTAINERS gives PAGE CACHE
               and XARRAY the same two lists, so the two stores hold the
               same mail.

Nothing here papers over that second point. Every scoped answer names the
archives it searched and the query korgalore built each one from, so a
caller can tell the maintainer "this is linux-fsdevel and linux-mm, shared
with XARRAY" instead of implying a precision that is not there.

Read-only, like the rest of the server: this parses files korgalore and
lei wrote, and writes none of them.
"""

import logging
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger('maintainer-mcp.subsystems')

# The feed kinds `kgl track-subsystem' can generate, most specific first
# so an unqualified request searches the narrow archive before the broad
# one. Either may be absent: generate_subsystem_config() takes
# include_patches and include_mailinglist, and a maintainer can turn
# either off.
KINDS: Tuple[str, ...] = ('patches', 'mailinglist')

# Long enough that a conversation's worth of tool calls reads the config
# once, short enough that tracking a new subsystem shows up without a
# restart. Nothing here is expensive; this is about not stat()ing a
# directory on every question.
CACHE_TTL = 300

# Said with every scoped answer, because the difference between the two
# kinds is the difference between a right answer and a confidently wrong
# one, and the caller is the one talking to the maintainer.
KIND_NOTE = (
    'A "patches" archive is built from the subsystem\'s F: file globs, so it is '
    'genuinely subsystem-scoped -- but it holds patches, and a bug report carries no '
    'diff, so it will not be in there. A "mailinglist" archive is built from the '
    "subsystem's L: lines, so it holds whole mailing lists: that is where bug reports, "
    'syzbot mail and build regressions are, and it is list-scoped rather than '
    'subsystem-scoped. Subsystems that share a mailing list therefore have identical '
    'mailinglist archives -- MAINTAINERS gives PAGE CACHE and XARRAY the same two -- so '
    'say which lists an answer came from rather than reporting it as specific to one '
    'subsystem.'
)


@dataclass(frozen=True)
class Store:
    """One subsystem's mail of one kind, as a searchable archive.

    *path* is a v2 public-inbox archive that lei wrote and indexed, and
    is what gets passed to lei as `-O'. *query* is the search korgalore
    built it from, carried along so an answer can show its own provenance
    rather than asking the caller to take the narrowing on trust.
    """

    subsystem: str
    key: str
    kind: str
    path: Path
    query: str = ''

    @property
    def name(self) -> str:
        """The archive's name on disk, as korgalore and lei both spell it."""
        return f'{self.key}-{self.kind}'

    @property
    def present(self) -> bool:
        """Whether this archive exists yet.

        False is ordinary rather than broken: korgalore writes the config
        when a subsystem is tracked and the directory when the first
        message for it arrives, so a freshly tracked subsystem sits in
        this state until the mail loop next runs.
        """
        return self.path.is_dir()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable view, provenance included."""
        found: Dict[str, Any] = {
            'subsystem': self.subsystem,
            'kind': self.kind,
            'archive': self.name,
            'present': self.present,
        }
        if self.query:
            found['built_from'] = self.query
        return found


@dataclass(frozen=True)
class Subsystem:
    """One tracked subsystem and the archives that hold its mail."""

    name: str
    key: str
    stores: Tuple[Store, ...] = ()

    def store(self, kind: str) -> Optional[Store]:
        """This subsystem's archive of one kind, or None if it has none."""
        for store in self.stores:
            if store.kind == kind:
                return store
        return None

    @property
    def kinds(self) -> Tuple[str, ...]:
        """The kinds this subsystem actually has, in KINDS order."""
        return tuple(kind for kind in KINDS if self.store(kind) is not None)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable view of this subsystem."""
        return {
            'name': self.name,
            'kinds': list(self.kinds),
            'archives': [store.to_dict() for store in self.stores],
        }


def _queries(saved_searches: Optional[Path]) -> Dict[str, str]:
    """Map each lei output path to the query that fills it.

    lei writes its saved searches as git-config, one directory per search,
    with the query under [lei] and the destination under [lei "q"]:

        [lei]
            q = (dfn:mm/filemap.c OR ...) AND d:90.days.ago..
        [lei "q"]
            output = v2:/data/xdg/data/korgalore/lei/page_cache-patches

    Read by line rather than through a git-config parser because two keys
    out of one flat file is not worth a dependency, and because the
    section a key sits in does not disambiguate anything here -- `q' and
    `output' appear once each.
    """
    found: Dict[str, str] = {}
    if saved_searches is None or not saved_searches.is_dir():
        return found

    for config in sorted(saved_searches.glob('*/lei.saved-search')):
        query = output = ''
        try:
            text = config.read_text(encoding='utf-8', errors='replace')
        except OSError as e:
            logger.warning('Could not read %s: %s', config, e)
            continue
        for line in text.splitlines():
            key, _, value = line.strip().partition(' = ')
            if key == 'q' and not query:
                query = value.strip()
            elif key == 'output' and not output:
                output = value.strip()
        if query and output:
            # `output' carries the format lei writes in (v2:, maildir:);
            # what identifies the archive is the path after it.
            found[output.partition(':')[2] or output] = query
    return found


def _read_config(path: Path, queries: Dict[str, str]) -> Optional[Subsystem]:
    """Turn one conf.d/<key>.toml into a Subsystem, or None if it is not one.

    The file korgalore generates names the subsystem once and then one
    feed per kind, each feed keyed `<key>-<kind>' and pointing at a
    `lei:<path>' URL. Files in conf.d that do not look like that -- a
    subscription, something hand-written -- are skipped rather than
    guessed at.
    """
    try:
        with path.open('rb') as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning('Could not read %s: %s', path, e)
        return None

    name = str((config.get('subsystem') or {}).get('name') or '')
    if not name:
        return None
    key = path.stem

    stores: List[Store] = []
    feeds = config.get('feeds') or {}
    for kind in KINDS:
        feed = feeds.get(f'{key}-{kind}')
        if not isinstance(feed, dict):
            continue
        url = str(feed.get('url') or '')
        if not url.startswith('lei:'):
            continue
        location = url[len('lei:') :]
        stores.append(
            Store(
                subsystem=name,
                key=key,
                kind=kind,
                path=Path(location),
                query=queries.get(location, ''),
            )
        )
    return Subsystem(name=name, key=key, stores=tuple(stores))


@dataclass
class Subsystems:
    """What this container tracks, read from korgalore's own config.

    *conf_d* is korgalore's conf.d, where `kgl track-subsystem' writes one
    file per subsystem, and *saved_searches* is lei's, read only to
    recover the query behind each archive. Both are optional: a container
    nobody has set up yet has neither, and that is reported as "nothing
    tracked" rather than raised, because an empty answer here is real
    information.
    """

    conf_d: Optional[Path] = None
    saved_searches: Optional[Path] = None
    ttl: int = CACHE_TTL
    _cache: Optional[Tuple[float, Tuple[Subsystem, ...]]] = field(default=None, repr=False)

    def all(self) -> Tuple[Subsystem, ...]:
        """Every tracked subsystem, by name, cached for *ttl* seconds."""
        now = time.monotonic()
        if self._cache is not None:
            cached_at, cached = self._cache
            if now - cached_at < self.ttl:
                return cached

        found: List[Subsystem] = []
        if self.conf_d is not None and self.conf_d.is_dir():
            queries = _queries(self.saved_searches)
            for path in sorted(self.conf_d.glob('*.toml')):
                subsystem = _read_config(path, queries)
                if subsystem is not None:
                    found.append(subsystem)
        found.sort(key=lambda subsystem: subsystem.name)

        tracked = tuple(found)
        self._cache = (now, tracked)
        return tracked

    def names(self) -> Tuple[str, ...]:
        """The tracked subsystem names, as MAINTAINERS spells them."""
        return tuple(subsystem.name for subsystem in self.all())

    def find(self, name: str) -> Optional[Subsystem]:
        """Look one subsystem up by name, or by the key korgalore filed it under.

        Case- and space-insensitive on the name, because a caller is
        reading it back from a result or from MAINTAINERS and should not
        have to match `LSILOGIC/SYMBIOS/NCR 53C8XX and 53C1010 PCI-SCSI
        drivers' exactly to get an answer.
        """
        wanted = ' '.join(name.split()).casefold()
        for subsystem in self.all():
            if subsystem.name.casefold() == wanted or subsystem.key.casefold() == wanted:
                return subsystem
        return None

    def resolve(self, names: Sequence[str], kinds: Sequence[str] = KINDS) -> Tuple[Store, ...]:
        """The archives to search for *names*, narrowed to *kinds*.

        Every name has to resolve. A typo that quietly searched three
        subsystems instead of four would come back as a confident, wrong,
        short answer -- so an unknown name raises and says what is
        tracked, which is also how a caller discovers the spelling.

        Raises:
            ValueError: if a name or kind is unknown, or nothing is tracked.
        """
        for kind in kinds:
            if kind not in KINDS:
                raise ValueError(f'kind={kind!r} is not one of: {", ".join(KINDS)}')

        tracked = self.all()
        if not tracked:
            raise ValueError(
                'This container is not tracking any subsystems yet, so there is '
                'nothing to narrow a search to. Tell the maintainer to track one '
                'at the container dashboard.'
            )

        found: List[Store] = []
        for name in names:
            subsystem = self.find(name)
            if subsystem is None:
                raise ValueError(
                    f'subsystem={name!r} is not tracked by this container. Tracked: {", ".join(self.names())}.'
                )
            for kind in kinds:
                store = subsystem.store(kind)
                if store is not None:
                    found.append(store)

        if not found:
            asked = ', '.join(kinds)
            raise ValueError(
                f'No {asked} archive exists for: {", ".join(names)}. '
                'Ask without a kind to search whatever this container does have.'
            )
        return tuple(found)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable view of everything tracked."""
        tracked = self.all()
        return {
            'subsystems': [subsystem.to_dict() for subsystem in tracked],
            'count': len(tracked),
            'kinds': list(KINDS),
            'note': KIND_NOTE,
        }
