#!/data/venv/bin/python
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
"""Initial setup web app for the maintainer container.

First run of the container drops the maintainer here: pick the subsystems
you're responsible for, and everything downstream (lei queries, tracked
lists, grokmirror manifests) is derived from those MAINTAINERS entries.

The localhost-only posture is enforced by how the
container publishes the port -- `-p 127.0.0.1:11043:11043` -- not by the bind
address in here, which has to be 0.0.0.0 for podman to forward to it at all.
Reach it from elsewhere over an ssh tunnel, not by publishing it wider.
"""

import configparser
import gzip
import json
import logging
import os
import re
import shutil
import socket
import stat
import subprocess
import threading
import time
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from korgalore import get_requests_session
from korgalore.cli import get_maintainers_file, get_xdg_config_dir, get_xdg_data_dir
from korgalore.maintainers import SubsystemEntry, Tree, normalize_subsystem_name, parse_maintainers


def env(name: str) -> str:
    """Read one of the container's shared settings.

    Values come from defaults.env, which entrypoint.sh sources and exports
    before anything starts, so the shell loops, router.psgi and this app all
    read the same paths and ports out of one file instead of each carrying
    its own copy. Missing means the environment wasn't set up, not that a
    fallback should be invented here -- to run this outside the container,
    `set -a; . defaults.env; set +a' first.
    """
    try:
        return os.environ[name]
    except KeyError:
        raise SystemExit(f'{name} is unset -- source defaults.env before running the dashboard') from None


# Internal-only from here on -- router.psgi is the one process bound to
# the externally-published port (0.0.0.0, see serve-web.sh) and proxies
# everything that isn't /lore, /cgit, or a git-clone path back to this
# app. 127.0.0.1 here is deliberate, not just a default: nothing but that
# proxy should ever reach this app directly.
HOST = env('DASHBOARD_HOST')
PORT = int(env('DASHBOARD_PORT'))
DATA_DIR = Path(env('DATA_DIR'))

# The MAINTAINERS file (and kernel.org's repo manifest) each have a few
# thousand entries and the search boxes are incremental, so cap what any
# one query hands back to the browser.
MAX_RESULTS = 50

# Refuse absurd request bodies rather than reading them into memory.
MAX_BODY = 1024 * 1024

INDEX_PATH = Path(__file__).parent / 'index.html'
SELECTION_PATH = DATA_DIR / 'selected-subsystems.json'
REPO_SELECTION_PATH = DATA_DIR / 'selected-repos.json'

# Who the maintainer is, asked for on the first screen. Kept next to the
# selections rather than in korgalore.toml because it is not korgalore's
# setting: it is the answer to "which of these messages are mine?", which
# the MCP server and anything else reading this volume needs as much as
# setup does.
IDENTITY_PATH = DATA_DIR / 'identity.json'

# Same cache path get_maintainers_file(DATA_DIR) already populated in
# load_subsystems() -- kgl's own default data dir is $XDG_DATA_HOME/korgalore,
# a different path, so without pointing it here explicitly it re-fetches
# MAINTAINERS from kernel.org a second time for every subsystem it tracks.
MAINTAINERS_PATH = DATA_DIR / 'MAINTAINERS'

KGL = env('KGL')

# grokmirror's pull-mode manifest of every repo kernel.org mirrors, keyed by
# repo path (e.g. "/pub/scm/linux/kernel/git/torvalds/linux.git") -- the
# same path a T: line's URL carries, just without the scheme/host. This is
# also the URL grok-pull itself would be configured to track, so it's the
# natural full list for the repo picker rather than something bespoke.
GROKMIRROR_SITE = env('GROKMIRROR_SITE')
MANIFEST_URL = f'{GROKMIRROR_SITE}/manifest.js.gz'
MANIFEST_HOST = urlparse(GROKMIRROR_SITE).hostname
MANIFEST_CACHE_PATH = DATA_DIR / 'grokmirror-manifest.json.gz'
MANIFEST_CACHE_MAX_AGE = 24 * 60 * 60
# How often refresh_loop() re-runs the loaders. Matched to the cache
# lifetime above so a pass lands just as the cached copies go stale.
REFRESH_INTERVAL = MANIFEST_CACHE_MAX_AGE

# Our own local mirror -- grok-pull's toplevel, config, and local manifest
# (distinct from the remote manifest above, which is just what we read to
# populate the picker). grok-pull is never run from here: grok-pull-loop.sh
# owns that process and only it may start one (its header explains what
# went wrong when two of them shared a toplevel). This app writes the
# config, and reads the pid file and log the loop leaves behind to report
# what the mirror is doing.
GROKMIRROR_DIR = Path(env('GROKMIRROR_DIR'))
GROKMIRROR_TOPLEVEL = Path(env('GROKMIRROR_TOPLEVEL'))
GROKMIRROR_OBJSTORE = Path(env('GROKMIRROR_OBJSTORE'))
GROKMIRROR_PRELOAD_DIR = Path(env('GROKMIRROR_PRELOAD_DIR'))
GROKMIRROR_CONF_PATH = Path(env('GROKMIRROR_CONF_PATH'))
GROKMIRROR_MANIFEST_PATH = Path(env('GROKMIRROR_MANIFEST_PATH'))
GROKMIRROR_PID_PATH = Path(env('GROKMIRROR_PID_PATH'))
GROKMIRROR_LOG_PATH = Path(env('GROKMIRROR_LOG_PATH'))
GROKMIRROR_SOCKET_PATH = Path(env('GROKMIRROR_SOCKET_PATH'))

# "Sync now". This app runs neither kgl pull nor grok-pull for it: it asks
# the loops that own them. The mail side is a request stamp that
# kgl-pull-loop.sh watches, plus a state file the loop writes back as each
# pass starts and ends. The git side is grok-pull's own socket (see
# request_mirror_sync).
SYNC_REQUEST_PATH = Path(env('SYNC_REQUEST_PATH'))
KGL_PULL_STATE_PATH = Path(env('KGL_PULL_STATE_PATH'))
PULL_STAMP_PATH = Path(env('PULL_STAMP_PATH'))

# How long to wait on grok-pull's socket before giving up on it. The
# listener accepts in a thread of its own and reads one line, so anything
# near this means it is not really there.
GROKMIRROR_SOCKET_TIMEOUT = 5

# Enough of grok-pull's log to show what it has been doing without handing
# a browser a whole clone's worth of output on its first poll.
MAX_MIRROR_LOG_BYTES = 256 * 1024

# Optional protocol listeners. Nothing is started from here: the choice is
# written to DAEMONS_PATH and serve-web.sh turns it into extra -l
# arguments for the public-inbox-netd it already runs, since that one
# process speaks IMAP and NNTP as well as HTTP.
#
# `hint' is the podman flag the maintainer still needs, and it is not a
# footnote: enabling a daemon binds its port inside the container, and
# nothing inside a container can publish a port to the host. Saying so on
# the screen is the difference between "I turned on IMAP and my mail
# client can't connect" and a working setup.
DAEMONS_PATH = Path(env('DAEMONS_PATH'))
# The address to tell the maintainer to publish an optional listener on.
# It cannot be derived from ROUTER_PUBLIC_BASE: that is a URL, and podman
# refuses a hostname in --publish ("cannot parse ... as an IP address"),
# so a container reached by name would be handed a flag that does not
# run. Nor can it be discovered -- a container cannot see its own port
# mappings. So it is told, and it defaults to the loopback that the
# documented `podman run' uses.
PUBLISH_ADDRESS = env('PUBLISH_ADDRESS')
IMAP_PORT = int(env('IMAP_PORT'))
NNTP_PORT = int(env('NNTP_PORT'))
DAEMONS: List[Dict[str, Any]] = [
    {
        'id': 'imap',
        'label': 'IMAP',
        'port': IMAP_PORT,
        'summary': 'Read the tracked archives from any mail client. Each inbox '
        'shows up as a folder; messages are read-only.',
    },
    {
        'id': 'nntp',
        'label': 'NNTP',
        'port': NNTP_PORT,
        'summary': 'The same archives as newsgroups, for a newsreader or for tools that speak NNTP rather than IMAP.',
    },
]
DAEMON_IDS = {daemon['id'] for daemon in DAEMONS}

# track-subsystem refuses to run at all unless korgalore.toml already has a
# target configured (see korgalore's main() in cli.py). This container
# browses mail rather than delivering it, and browse mode doesn't need a
# delivery copy of messages at all -- the lei
# v2 archive underneath the feeds already holds everything. Seed a no-op
# dummy target here so track-subsystem has something to point at without
# asking the maintainer for any credentials or duplicating mail storage.
DEFAULT_TARGET = 'local'

# public-inbox's own config -- kept separate from korgalore.toml/
# grokmirror.conf since it's neither korgalore's nor grokmirror's, and
# public-inbox-extindex needs a stable PI_CONFIG to read the same
# [publicinbox "..."] sections from. topdir holds the actual Xapian
# cross-inbox index that backs the /all/ endpoint.
#
# Deliberately no url= on the sections written below, matching
# lore.kernel.org's own config. public-inbox only consults it outside a
# web request: PublicInbox::Inbox::base_url prefers the address the
# browser actually used whenever it has a PSGI env, and falls back to
# url= only when there is none. Setting it therefore changes exactly one
# thing, and for the worse -- WwwListing (line 21) links each inbox by
# url= when it is set and by a relative `name/' when it is not, so the
# /lore/ front page ends up sending every reader to whichever single
# address happened to be configured.
#
# That matters here more than it does upstream, because one container is
# routinely reached at several addresses: loopback on the host, a VPN
# name from another machine, and 127.0.0.1 again through an ssh forward.
# Leaving url= out makes every link follow the address in the request,
# so all three work without the config knowing about any of them.
# ViewVCS, WwwCoderepo and NewsWWW all fall back the same way.
PUBLICINBOX_CONFIG_PATH = Path(env('PI_CONFIG'))
PUBLICINBOX_DIR = PUBLICINBOX_CONFIG_PATH.parent
PUBLICINBOX_EXTINDEX_PATH = Path(env('PUBLICINBOX_EXTINDEX_PATH'))
EXTINDEX_STAMP_PATH = Path(env('EXTINDEX_STAMP_PATH'))
ROUTER_PUBLIC_BASE = env('ROUTER_PUBLIC_BASE')

# PublicInbox::Cgit locates the cgit CGI binary itself from a fixed list of
# common install paths, none of which match where the lfit copr's cgit
# package actually puts it -- so it has to be pointed at explicitly.
CGIT_BIN = '/var/www/cgi-bin/cgit'
CGITRC_PATH = '/etc/cgitrc'

# public-inbox's own example stylesheets (CC0-1.0, from its contrib/css) --
# see the css= lines write_publicinbox_config appends below.
PUBLICINBOX_CSS_DIR = '/etc/public-inbox/css'

logger = logging.getLogger('dashboard')

SUBSYSTEMS: Dict[str, SubsystemEntry] = {}
MANIFEST: Dict[str, Dict[str, Any]] = {}

# Both tables are filled in by a background thread so the dashboard can bind
# its port and serve the page immediately. On a cold start both loaders go
# to the network -- MAINTAINERS is around 750KB and the grokmirror manifest
# a few MB -- and doing that before listening meant the whole container
# looked down for as long as it took, with the router having nothing to
# proxy to. The events below mark each table as usable; the handful of
# routes that read one waits on it rather than answering from an empty
# table, which would look like "no such subsystem" or quietly drop a saved
# repo selection that only fails the `path in MANIFEST' test because the
# manifest isn't there yet.
SUBSYSTEMS_READY = threading.Event()
MANIFEST_READY = threading.Event()

# Long enough to cover a slow fetch, short enough that a client isn't left
# hanging forever if the network is simply gone. Both loaders fall back to a
# cached (or empty) copy rather than retrying indefinitely, so in practice
# the events get set well inside this.
READY_TIMEOUT = 120


def load_subsystems() -> Dict[str, SubsystemEntry]:
    """Parse MAINTAINERS, fetching it from kernel.org if the cache is stale."""
    path = get_maintainers_file(DATA_DIR)
    entries = parse_maintainers(path)
    logger.info('Loaded %d subsystems from %s', len(entries), path)
    return entries


def read_cached_manifest() -> Dict[str, Dict[str, Any]]:
    """Decompress and parse the on-disk grokmirror manifest cache."""
    manifest: Dict[str, Dict[str, Any]] = json.loads(gzip.decompress(MANIFEST_CACHE_PATH.read_bytes()))
    return manifest


def load_manifest() -> Dict[str, Dict[str, Any]]:
    """Fetch kernel.org's grokmirror manifest, using a cached copy if fresh.

    Mirrors get_maintainers_file()'s cache-for-a-day-then-refetch approach,
    including falling back to a stale cache (or an empty manifest, if
    there's no cache at all yet) rather than failing the whole dashboard
    over a manifest fetch hiccup -- the repo picker just comes up empty.
    """
    if MANIFEST_CACHE_PATH.exists():
        age = time.time() - MANIFEST_CACHE_PATH.stat().st_mtime
        if age < MANIFEST_CACHE_MAX_AGE:
            logger.debug('Using cached grokmirror manifest (age: %.1f hours)', age / 3600)
            return read_cached_manifest()
        logger.debug('Cached grokmirror manifest is stale (age: %.1f hours)', age / 3600)

    logger.info('Fetching grokmirror manifest from %s', MANIFEST_URL)
    try:
        session = get_requests_session()
        response = session.get(MANIFEST_URL, timeout=30)
        response.raise_for_status()
        raw = response.content
    except Exception as e:
        if MANIFEST_CACHE_PATH.exists():
            logger.warning('Failed to fetch fresh grokmirror manifest, using stale cache: %s', e)
            return read_cached_manifest()
        logger.warning('Failed to fetch grokmirror manifest, repo picker will start empty: %s', e)
        return {}

    MANIFEST_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_CACHE_PATH.write_bytes(raw)
    logger.debug('Cached grokmirror manifest to %s', MANIFEST_CACHE_PATH)
    manifest: Dict[str, Dict[str, Any]] = json.loads(gzip.decompress(raw))
    logger.info('Loaded %d repos from grokmirror manifest', len(manifest))
    return manifest


def refresh_loop() -> None:
    """Load MAINTAINERS and the grokmirror manifest, then again once a day.

    Both loaders cache for a day and re-fetch when the cache goes stale, but
    that logic only ever ran at startup: a container that stays up for weeks
    -- which is the normal case -- would keep serving whatever the subsystem
    list and repo picker looked like the moment it booted, no matter how
    much either had moved on upstream.

    Each global is rebound as a whole, never mutated in place, so a request
    being served during a refresh reads either the old table or the new one
    and never a half-built one. A failed refresh leaves the previous
    contents in place and is retried on the next pass.
    """
    global SUBSYSTEMS, MANIFEST

    # First pass runs immediately: this is also the initial load, done here
    # rather than before the server binds (see main). The events are set even
    # when a loader fails, since its fallback -- a stale cache, or an empty
    # table -- is the answer, and making requests wait out READY_TIMEOUT for
    # something that isn't coming would just turn a degraded dashboard into
    # an unresponsive one.
    while True:
        try:
            SUBSYSTEMS = load_subsystems()
        except Exception as e:
            logger.warning('Failed to load the subsystem list, keeping the previous one: %s', e)
        SUBSYSTEMS_READY.set()

        try:
            MANIFEST = load_manifest()
        except Exception as e:
            logger.warning('Failed to load the grokmirror manifest, keeping the previous one: %s', e)
        MANIFEST_READY.set()

        time.sleep(REFRESH_INTERVAL)


def tree_manifest_path(tree: Tree) -> Optional[str]:
    """Resolve a T: line to a manifest path, if it points at our mirror site.

    T: lines mix git:// and https:// (and the occasional bare URL with no
    vcs keyword at all) for what's often the same underlying repo, so match
    on host + path rather than the URL string -- that's also exactly how
    grokmirror itself keys the manifest, path relative to its site.
    """
    parsed = urlparse(tree.url)
    if parsed.hostname != MANIFEST_HOST:
        return None
    path = parsed.path.rstrip('/')
    return path or None


def summarize_repo(path: str, info: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a manifest entry to what the picker needs to show."""
    return {
        'path': path,
        'description': info.get('description'),
        'owner': info.get('owner'),
    }


def search_repos(query: str) -> List[Dict[str, Any]]:
    """Find manifest repos whose path or description matches query."""
    needle = query.strip().lower()
    if not needle:
        return []

    matches = [
        (path, info)
        for path, info in MANIFEST.items()
        if needle in path.lower() or (info.get('description') and needle in info['description'].lower())
    ]
    # A path whose final component starts with what was typed is what the
    # maintainer most likely meant, so float those above mid-path matches.
    matches.sort(key=lambda pi: (not pi[0].rsplit('/', 1)[-1].lower().startswith(needle), pi[0]))

    return [summarize_repo(path, info) for path, info in matches[:MAX_RESULTS]]


LINUX_PATH = '/pub/scm/linux/kernel/git/torvalds/linux.git'


def suggested_repos() -> List[Dict[str, Any]]:
    """Repos to pre-check: mainline Linux, plus T: lines of the selected subsystems.

    Almost every maintainer wants Linus's tree alongside whatever they're
    tracking, so it's suggested unconditionally. Only trees that resolve to
    something actually in kernel.org's manifest are suggested otherwise -- a
    subsystem's tree can live on github.com or elsewhere entirely outside
    what grokmirror here would ever mirror.
    """
    paths: List[str] = []
    seen = set()
    if LINUX_PATH in MANIFEST:
        seen.add(LINUX_PATH)
        paths.append(LINUX_PATH)
    for name in read_selection():
        entry = SUBSYSTEMS.get(name)
        if not entry:
            continue
        for tree in entry.trees:
            path = tree_manifest_path(tree)
            if path and path in MANIFEST and path not in seen:
                seen.add(path)
                paths.append(path)

    return [summarize_repo(path, MANIFEST[path]) for path in paths]


def search(query: str) -> List[Dict[str, Any]]:
    """Find subsystems whose name matches query, best matches first."""
    needle = query.strip().lower()
    if not needle:
        return []

    matches = [entry for entry in SUBSYSTEMS.values() if needle in entry.name.lower()]
    # Names that start with what was typed are what the maintainer most
    # likely meant, so float those above mid-name matches.
    matches.sort(key=lambda entry: (not entry.name.lower().startswith(needle), entry.name))

    return [summarize(entry) for entry in matches[:MAX_RESULTS]]


def summarize(entry: SubsystemEntry) -> Dict[str, Any]:
    """Reduce an entry to what the picker needs to show."""
    return {
        'name': entry.name,
        'lists': entry.mailing_lists,
        'maintainers': entry.maintainers,
        'status': entry.status,
    }


# A pasted MAINTAINERS line brings its field prefix along, and typing the
# address out by hand does not. Both are the same answer.
IDENTITY_PREFIX = re.compile(r'\A[MR]:\s*')


def parse_identity(raw: str) -> Tuple[str, str]:
    """Read `(name, address)' out of whatever the maintainer typed.

    The question on screen names the MAINTAINERS file, so the obvious
    thing to do is paste the line out of it -- with or without the `M:'
    in front, with or without the display name. All of those are
    accepted, and a bare address is too.

    Validation stays deliberately shallow. This is not an attempt to
    decide what a legal address is; it is a check that what came back can
    be compared against MAINTAINERS at all, so that a stray word or an
    empty box is caught here rather than silently matching nothing.

    Raises:
        ValueError: If no usable address can be read out of *raw*.
    """
    name, address = parseaddr(IDENTITY_PREFIX.sub('', raw.strip()))
    address = address.strip()
    local, _, domain = address.partition('@')
    if not local or not domain or '@' in domain or any(c.isspace() for c in address):
        raise ValueError(f'{raw.strip()!r} does not look like an email address')
    return ' '.join(name.split()), address


def read_identity() -> Dict[str, str]:
    """Load the saved identity, treating anything unreadable as unanswered."""
    try:
        saved = json.loads(IDENTITY_PATH.read_text())
    except FileNotFoundError:
        return {'name': '', 'address': ''}
    except (OSError, ValueError):
        logger.warning('Ignoring unreadable identity at %s', IDENTITY_PATH)
        return {'name': '', 'address': ''}

    return {
        'name': str(saved.get('name', '')),
        'address': str(saved.get('address', '')),
    }


def write_identity(raw: str) -> Dict[str, str]:
    """Save who the maintainer is.

    Raises:
        ValueError: If *raw* holds no usable address; nothing is written.
    """
    name, address = parse_identity(raw)
    identity = {'name': name, 'address': address}
    IDENTITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    IDENTITY_PATH.write_text(json.dumps(identity, indent=2) + '\n')
    logger.info('Saved the maintainer identity %s to %s', address, IDENTITY_PATH)
    return identity


def subsystems_for(address: str) -> List[Dict[str, Any]]:
    """Find the subsystems that list *address*, maintainer or reviewer.

    Both count. An R: entry gets the same mail as the M: one above it and
    is expected to read it, so a reviewer who only got the subsystems
    they maintain would be handed an archive missing the very threads
    they were listed for.

    Addresses are compared case-insensitively on the whole string: the
    local part is technically case-sensitive, but nobody writes their own
    address in MAINTAINERS one way and types it another meaning two
    different people, and matching nothing at all is the worse failure.
    """
    needle = address.lower()
    found = []
    for entry in SUBSYSTEMS.values():
        if needle in [addr.lower() for addr in entry.maintainers]:
            role = 'maintainer'
        elif needle in [addr.lower() for addr in entry.reviewers]:
            role = 'reviewer'
        else:
            continue
        found.append({**summarize(entry), 'role': role})

    found.sort(key=lambda item: str(item['name']))
    logger.info('%s is listed on %d subsystem(s)', address, len(found))
    return found


def identity_state() -> Dict[str, Any]:
    """The saved identity together with what it currently matches.

    Matches are worked out on every read rather than saved alongside the
    address, because MAINTAINERS is re-fetched daily: a maintainer who
    picks up a subsystem next month should see it offered without having
    to answer this screen again.
    """
    identity = read_identity()
    matches = subsystems_for(identity['address']) if identity['address'] else []
    return {'identity': identity, 'matches': matches}


def read_selection() -> List[str]:
    """Load the saved selection, treating anything unreadable as empty."""
    try:
        saved = json.loads(SELECTION_PATH.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning('Ignoring unreadable selection at %s', SELECTION_PATH)
        return []

    # Entries can disappear between runs when MAINTAINERS is refreshed.
    return [name for name in saved.get('selected', []) if name in SUBSYSTEMS]


def write_selection(names: List[str]) -> List[str]:
    """Save the selection, keeping only names that exist in MAINTAINERS."""
    known = [name for name in names if name in SUBSYSTEMS]
    SELECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SELECTION_PATH.write_text(json.dumps({'selected': known}, indent=2) + '\n')
    logger.info('Saved %d selected subsystems to %s', len(known), SELECTION_PATH)
    return known


def read_repo_selection() -> List[str]:
    """Load the saved repo selection, treating anything unreadable as empty."""
    try:
        saved = json.loads(REPO_SELECTION_PATH.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning('Ignoring unreadable repo selection at %s', REPO_SELECTION_PATH)
        return []

    # Entries can disappear between runs when the manifest is refreshed.
    return [path for path in saved.get('selected', []) if path in MANIFEST]


def write_repo_selection(paths: List[str]) -> List[str]:
    """Save the repo selection, keeping only paths that exist in the manifest."""
    known = [path for path in paths if path in MANIFEST]
    REPO_SELECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPO_SELECTION_PATH.write_text(json.dumps({'selected': known}, indent=2) + '\n')
    logger.info('Saved %d selected repos to %s', len(known), REPO_SELECTION_PATH)
    return known


def ensure_default_target() -> None:
    """Make sure korgalore.toml exists with at least one target.

    korgalore refuses to run any command until a target is configured, and
    real deliver-mode targets (Gmail, JMAP, IMAP) are a maintainer decision
    this screen doesn't ask for yet. Only writes a file that isn't there --
    never touches a config the maintainer already has.
    """
    cfgpath = get_xdg_config_dir() / 'korgalore.toml'
    if cfgpath.exists():
        return

    cfgpath.parent.mkdir(parents=True, exist_ok=True)
    cfgpath.write_text(f"[targets.{DEFAULT_TARGET}]\ntype = 'dummy'\n")
    logger.info('Seeded default dummy target in %s', cfgpath)


# Tracking subsystems runs detached from the request that starts it: one
# `kgl track-subsystem' spends most of its time inside a single lei query,
# and a maintainer importing six months of a busy list waits minutes per
# subsystem. /api/generate starts the run and returns; /api/generate/status
# is polled for progress. Mirroring is handed off in the same spirit,
# though it goes further and leaves the process to grok-pull-loop.sh
# entirely -- see the mirror section below.
#
# It is not a held-open stream (NDJSON or the like) on purpose. There the
# connection is load bearing: the work is a generator driven by the writes
# to the socket, so anything that breaks the connection -- a closed tab, a
# reload, the router's proxy timing out on a lei query that produced no
# output for a minute -- abandons the generator mid-run. What makes that
# costly rather than merely annoying is that the remaining subsystems are
# then never tracked and write_publicinbox_config() never runs, so setup
# ends up silently half-finished, with no inbox config at all and nothing
# saying so.
#
# Events are buffered rather than flat lines (each carries the subsystem it
# belongs to) so a page that arrives late can still rebuild per-subsystem
# progress. The cap and the absolute cursor work exactly as they do for the
# mirror buffer; see mirror_status.
_generate_lock = threading.Lock()
_generate_state: Dict[str, Any] = {'status': 'idle', 'events': [], 'ok': None}
MAX_GENERATE_EVENTS = 2000
_generate_dropped = 0


def start_generate(names: List[str], since: str) -> bool:
    """Kick off a tracking run in the background. False if one's already running."""
    global _generate_dropped
    with _generate_lock:
        if _generate_state['status'] == 'running':
            return False
        _generate_state['status'] = 'running'
        _generate_state['events'] = []
        _generate_state['ok'] = None
        _generate_dropped = 0

    threading.Thread(target=_run_generate, args=(names, since), daemon=True).start()
    return True


def imported_subsystems(names: List[str]) -> List[str]:
    """Which of these subsystems already have archives on disk.

    Run state lives in this process and nothing else, so it resets every
    time the container restarts -- ask it whether the archives are there
    and it says 'idle', which reads the same as 'never imported'. The
    archives themselves are on the volume and outlast all of that, so they
    are what gets asked instead.

    Same naming rule as write_publicinbox_config, and for the same reason:
    `kgl track-subsystem' derives the directory from the subsystem name, so
    its presence is the answer rather than a guess about one.
    """
    lei_base_path = get_xdg_data_dir() / 'lei'
    imported = []
    for name in names:
        if name not in SUBSYSTEMS:
            continue
        key = normalize_subsystem_name(name)
        if any((lei_base_path / f'{key}-{suffix}').exists() for suffix in ('mailinglist', 'patches')):
            imported.append(name)
    return imported


def generate_status(since: int) -> Dict[str, Any]:
    """Snapshot of the background run for /api/generate/status polling."""
    # Outside the lock: it touches the filesystem, and it has nothing to do
    # with the run state the lock protects.
    imported = imported_subsystems(read_selection())
    with _generate_lock:
        start = max(0, since - _generate_dropped)
        return {
            'status': _generate_state['status'],
            'events': _generate_state['events'][start:],
            'next': _generate_dropped + len(_generate_state['events']),
            'ok': _generate_state['ok'],
            'imported': imported,
        }


def _run_generate(names: List[str], since: str) -> None:
    """Run `kgl track-subsystem` for each selected subsystem, one at a time.

    Records events as they happen -- 'start' when a subsystem's command
    launches, 'line' for each line of its combined stdout/stderr as it's
    produced, and 'done' with the final result -- so the UI can show
    progress instead of leaving the maintainer staring at a blank screen
    for however long lei takes.

    Runs in its own thread; progress and outcome go into _generate_state
    rather than being returned, since nothing is left waiting on this call.
    """

    def add_event(event: Dict[str, Any]) -> None:
        global _generate_dropped
        with _generate_lock:
            _generate_state['events'].append(event)
            excess = len(_generate_state['events']) - MAX_GENERATE_EVENTS
            if excess > 0:
                del _generate_state['events'][:excess]
                _generate_dropped += excess

    def finish(ok: bool) -> None:
        with _generate_lock:
            _generate_state['status'] = 'done' if ok else 'failed'
            _generate_state['ok'] = ok

    try:
        ensure_default_target()

        failed = 0
        for name in names:
            add_event({'type': 'start', 'name': name})

            if name not in SUBSYSTEMS:
                add_event({'type': 'line', 'name': name, 'text': 'Unknown subsystem (MAINTAINERS may have changed).'})
                add_event({'type': 'done', 'name': name, 'ok': False})
                failed += 1
                continue

            # --threads, because the mirror exists to be read by b4. A
            # bare query matches individual messages, so a series comes in
            # as whatever subset of it happened to match: patch 3 of 7
            # without its cover letter, a reply without the patch it is
            # replying to. b4 needs the whole thread to reassemble a
            # series, and `lei q --threads' pulls every message of a
            # thread once any one of them matches -- including the ones
            # that fall outside --since, which is what makes a series
            # straddling the window boundary arrive whole.
            #
            # It costs more: more messages fetched, a bigger archive. That
            # is the trade being made deliberately, not an oversight to
            # tidy up later.
            #
            # The partial-mirror headers depend on it too. Once the pull
            # stamp is fresh, a client trusts this mirror to have every new
            # reply to a thread it has (router.psgi, X-Archive-Coverage).
            # The list query gets those on its own, since replies go to the
            # list. The patches query matches files, and a plain reply
            # touches none -- only --threads brings the review in after the
            # patch. Dropping it would make clients miss replies without
            # any sign. korgalore defaults to --no-threads, so this flag is
            # the only thing that keeps the promise.
            # (lore-partial-mirrors.md, section 3.2.1.)
            proc = subprocess.Popen(
                [
                    KGL,
                    'track-subsystem',
                    name,
                    '--target',
                    DEFAULT_TARGET,
                    '--since',
                    since,
                    '--threads',
                    '--maintainers',
                    str(MAINTAINERS_PATH),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                add_event({'type': 'line', 'name': name, 'text': line.rstrip('\n')})

            returncode = proc.wait()
            ok = returncode == 0
            if not ok:
                logger.warning('track-subsystem failed for %r (exit %d)', name, returncode)
                failed += 1
            add_event({'type': 'done', 'name': name, 'ok': ok})

        # Written for whatever got tracked, even if some subsystems failed:
        # an inbox config covering the ones that worked is more useful than
        # none, and a later re-run just rewrites it.
        write_publicinbox_config(names)
        finish(failed == 0)
    except Exception as e:
        # Nothing is waiting on this thread's return value, so a crash here
        # would otherwise vanish silently and leave status stuck at
        # 'running' forever -- surface it the same way a failed run is.
        logger.exception('track-subsystem background worker crashed')
        add_event({'type': 'line', 'name': '', 'text': f'Internal error: {e}'})
        finish(False)


# How long to let a config check run. The offline check is all stat()
# calls and finishes instantly; the online one fetches the remote manifest
# and is the reason there is a timeout at all.
CONFIG_CHECK_TIMEOUT = 60
# Reading a handful of keys out of a local file; a timeout at all is only
# here so a wedged git can never stall the sync loop.
CONFIG_READ_TIMEOUT = 30


def check_grokmirror_config(online: bool = False) -> Dict[str, Any]:
    """Ask grok-pull what is wrong with the config we just generated.

    `grok-pull --config-check' reports every problem it finds rather than
    dying on the first, writes nothing, and runs no configured command --
    so it is safe against a mirror that is already running, which is the
    normal state here: grok-pull-loop.sh is up from container start.

    Worth doing even though we generate the config ourselves, precisely
    because we generate it: what this validates is our own generator, and
    a check that only ever ran against hand-written configs would never
    have caught anything we did.

    Returns the report as grokmirror's own JSON object (see
    configcheck.format_json), with `ok' false and a synthesized diagnostic
    if the check could not be run at all -- an older grokmirror without
    the flag, or a timeout. Never raises: a broken checker must not take
    down the thing it was checking.
    """
    argv = ['grok-pull', '-c', str(GROKMIRROR_CONF_PATH), '--config-check', '--json']
    if not online:
        argv.append('--no-network')

    def unavailable(message: str, hint: Optional[str] = None) -> Dict[str, Any]:
        return {
            'config': str(GROKMIRROR_CONF_PATH),
            'checked_as': None,
            'online': online,
            'ok': False,
            'diagnostics': [
                {
                    'severity': 'error',
                    'section': None,
                    'option': None,
                    'message': message,
                    'hint': hint,
                }
            ],
            'summary': {'errors': 1, 'warnings': 0},
            'unavailable': True,
        }

    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=CONFIG_CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        return unavailable(
            f'grok-pull --config-check did not finish within {CONFIG_CHECK_TIMEOUT}s',
            'The online check fetches the remote manifest; the remote site may be unreachable.' if online else None,
        )
    except OSError as e:
        return unavailable(f'could not run grok-pull: {e}')

    try:
        report: Dict[str, Any] = json.loads(proc.stdout)
    except ValueError:
        # argparse exits 2 with usage on stderr for an unrecognised flag,
        # which is what a grokmirror predating --config-check looks like.
        # Not an error in the config, so say what it actually is.
        detail = (proc.stderr or proc.stdout or '').strip().splitlines()
        return unavailable(
            f'grok-pull --config-check produced no report (exit {proc.returncode})',
            detail[-1] if detail else 'This grokmirror may predate --config-check.',
        )

    # run_check exits 1 only when something is an error; warnings never
    # fail it. `ok' in the report says the same thing, so the exit code is
    # not consulted separately.
    return report


def log_grokmirror_config_check(report: Dict[str, Any]) -> None:
    """Log a config-check report at a level that matches what it found."""
    for diag in report.get('diagnostics', []):
        where = ' '.join(
            filter(
                None,
                (
                    f'[{diag["section"]}]' if diag.get('section') else '',
                    f'{diag["option"]}:' if diag.get('option') else '',
                ),
            )
        )
        line = f'grokmirror config: {where} {diag["message"]}'.replace('  ', ' ')
        if diag.get('severity') == 'error':
            logger.error('%s', line)
        else:
            logger.warning('%s', line)
        if diag.get('hint'):
            logger.info('grokmirror config:   %s', diag['hint'])
    if report.get('ok') and not report.get('diagnostics'):
        logger.info('grokmirror config checks out (%s)', 'online' if report.get('online') else 'offline')


def find_preloads() -> List[str]:
    """Upstream paths with a finished preload under GROKMIRROR_PRELOAD_DIR.

    preload-objstore.sh lays them out by upstream path, the way the toplevel
    is, and builds each one under a dot-name first -- so a dot-name is one
    that never finished, and is not a preload.
    """
    found = []
    for root, dirs, _files in os.walk(GROKMIRROR_PRELOAD_DIR):
        for name in list(dirs):
            if name.startswith('.'):
                dirs.remove(name)
            elif name.endswith('.git'):
                # A repo, not a directory of them: nothing further down.
                dirs.remove(name)
                rel = Path(root, name).relative_to(GROKMIRROR_PRELOAD_DIR)
                found.append(f'/{rel.as_posix()}')
    return sorted(found)


def adopt_preloads(paths: List[str]) -> List[str]:
    """Move preloaded object stores into place for the repos about to be pulled.

    preload-objstore.sh builds one out of a clone the maintainer already had
    and leaves it under GROKMIRROR_PRELOAD_DIR by upstream path. grokmirror
    keeps one object store per forkgroup, named after it, and a repo whose
    forkgroup already has one is cloned against it -- grok-pull then fetches
    only what is missing. So adopting is just a rename, and it has to happen
    before grok-pull sees the repo, which is before the config that names it
    is written.

    It is the forkgroup that has to match, not the path. Nearly every kernel
    tree is in mainline's forkgroup, so a maintainer who preloads mainline
    and then mirrors only their own subsystem tree gets the benefit anyway.

    A preload for a forkgroup that already has an object store can never be
    used, and a Linux one is several GB, so it is removed rather than left
    for somebody to find later. One for a forkgroup nothing picked yet is
    left alone: the maintainer may still pick it.

    Returns the upstream paths of the preloads that were adopted.

    There is a window grokmirror has too (fsck.py says so, next to the
    rmtree): grok-fsck deletes an object store no repo borrows from yet. It
    works from the list of object stores it took when it started, so the
    only exposure is to one starting in the second or two between this and
    grok-pull setting up the new repo's alternates -- and grok-fsck-loop.sh
    runs it once a day. Losing that race costs a full download, not data.
    """
    wanted = {MANIFEST[path].get('forkgroup') for path in paths if path in MANIFEST}
    adopted = []
    for upstream in find_preloads():
        preload = GROKMIRROR_PRELOAD_DIR / upstream.lstrip('/')
        forkgroup = MANIFEST.get(upstream, {}).get('forkgroup')
        if not forkgroup:
            logger.warning('Not using the preload for %s: the manifest gives it no forkgroup', upstream)
            continue
        if forkgroup not in wanted:
            continue
        objstore = GROKMIRROR_OBJSTORE / f'{forkgroup}.git'
        if objstore.exists():
            logger.info('Removing the preload for %s: its object store is already there', upstream)
            shutil.rmtree(preload)
            continue
        GROKMIRROR_OBJSTORE.mkdir(parents=True, exist_ok=True)
        preload.rename(objstore)
        logger.info('Using the preload for %s as object store %s', upstream, objstore)
        adopted.append(upstream)
    return adopted


def write_grokmirror_config(paths: List[str]) -> Dict[str, Any]:
    """Write grokmirror.conf so grok-pull mirrors exactly the selected repos.

    Returns the config-check report for what was just written, so a caller
    that wants to show it to somebody does not have to run the check a
    second time.

    `include` takes the selected paths literally (each is also a valid glob
    with no special characters), so grok-pull mirrors those and nothing
    else -- and with purge on, cleans up anything left over from a repo the
    maintainer later unchecks.
    """
    GROKMIRROR_TOPLEVEL.mkdir(parents=True, exist_ok=True)

    config = configparser.ConfigParser()
    config['core'] = {
        'toplevel': str(GROKMIRROR_TOPLEVEL),
        'manifest': str(GROKMIRROR_MANIFEST_PATH),
        'log': str(GROKMIRROR_LOG_PATH),
        'loglevel': 'info',
        # Out of the toplevel on purpose (grokmirror would default it to
        # $toplevel/objstore). Object storage repos have no refs of their
        # own worth browsing, but cgit's scan-path and router.psgi's clone
        # route both find repos by walking the toplevel, so down there they
        # show up in the cgit index and answer git clone. grokmirror itself
        # never discovers them by walking -- pull.py and fsck.py both read
        # this setting -- so moving them costs it nothing.
        'objstore': str(GROKMIRROR_OBJSTORE),
        # Fetch objstore alternates via git's plumbing instead of the
        # default fetch-then-repack -Adlq -- same setting git.kernel.org's
        # own config uses, and it skips exactly the full repack that made
        # v9fs.git's initial clone take most of its 25 minutes in testing.
        'objstore_uses_plumbing': 'yes',
    }
    config['remote'] = {
        'site': GROKMIRROR_SITE,
        'manifest': MANIFEST_URL,
    }
    config['pull'] = {
        'purge': 'yes',
        'pull_threads': '5',
        'retries': '3',
        'include': '\n\t'.join(paths),
        # What "Sync now" writes repo paths into. Without it, the only way
        # to get grok-pull to look again before the next refresh is to
        # restart it, and a restart in the middle of a first clone throws
        # that clone away.
        'socket': str(GROKMIRROR_SOCKET_PATH),
        # Only takes effect under grok-pull -o -- grok-pull-loop.sh runs the
        # initial import the dashboard triggers, then keeps re-checking the
        # manifest on this interval for as long as the container is up.
        'refresh': '300',
    }
    config['fsck'] = {
        'statusfile': str(GROKMIRROR_DIR / 'fsck-status.json'),
    }

    with GROKMIRROR_CONF_PATH.open('w') as f:
        config.write(f)
    logger.info('Wrote grokmirror config for %d repo(s) to %s', len(paths), GROKMIRROR_CONF_PATH)

    # Check it before grok-pull-loop.sh picks the new mtime up and acts on
    # it. Offline: the remote checks want the network and would make
    # writing a config depend on git.kernel.org being reachable, which is
    # grok-pull's own problem to report and not a reason to hold up
    # setup. The dashboard can ask for the online check on demand.
    #
    # Nothing here blocks on the result. An error means the next pull will
    # fail, and it will fail with this in the log right above it -- which
    # is the whole point, since otherwise the first sign of a bad config
    # is a mirror that silently never populates.
    report = check_grokmirror_config(online=False)
    log_grokmirror_config_check(report)
    return report


# Newsgroup names are how IMAP and NNTP address an inbox, and public-inbox
# skips any inbox that has not got one. Both daemons open their refresh
# callback with the same line -- IMAPD.pm:28 and NNTPD.pm:38:
#
#     my $ngname = $ibx->{newsgroup} // return;
#
# A bare return: no warning, no error, the inbox simply never appears in
# the folder or group list. So an archive with no newsgroup key still
# serves fine over HTTP and is invisible over IMAP and NNTP, and the
# daemons bind and greet normally either way -- the greeting happens long
# before any mailbox lookup. That makes it a considerably more confusing
# failure than refusing to start would be.
#
# The `local.' prefix keeps us out of the way of real upstream names.
# lore.kernel.org publishes its own groups under reverse-DNS
# (org.kernel.linux.tools for the tools list, and so on), so a container
# that someday also fronts lore over NNTP has no collision to resolve.
NEWSGROUP_PREFIX = 'local.maintainer'

# What public-inbox will accept. PublicInbox::Config drops a newsgroup
# whose name is empty or has a character outside this set (Config.pm:522,
# chosen for RFC 3977 wildmat-exact and RFC 3501 ATOM-CHAR), and
# lowercases anything else with a warning. PublicInbox::IMAPD separately
# refuses a trailing all-numeric component -- it slices large mailboxes
# into name.1, name.2, ... and a group actually named that way would be
# ambiguous with a slice.
#
# Empty dot-separated components are rejected on our own account rather
# than public-inbox's: Config.pm's character check happily accepts a name
# like `local.maintainer.usb.' because a dot is a legal character and the
# string as a whole is not empty, so nothing downstream would complain
# about a group with a nameless component.
#
# Neither upstream rule can bite as things stand: normalize_subsystem_name yields
# [a-z0-9_]+, and the last component is always a word suffix, so the
# numeric tail is structurally unreachable. Both are checked anyway. The
# cost is two regex matches per inbox, and the thing they guard against
# is an empty folder list with nothing logged.
NEWSGROUP_INVALID_CHARS = re.compile(r'[^a-z0-9/_.~@+=:-]')
NEWSGROUP_NUMERIC_TAIL = re.compile(r'\.[0-9]+\Z')


def newsgroup_name(key: str, suffix: str) -> Optional[str]:
    """Build the newsgroup name for one inbox, or None if it is unusable.

    The suffix is joined with a dot rather than the hyphen used in the
    inbox name, so IMAP nests it: PublicInbox::IMAPD walks a dotted name
    up to its root creating a selectable dummy at each level, which turns
    local.maintainer.usb.mailinglist and .patches into two children of a
    local/maintainer/usb folder instead of two flat siblings.

    Returns None rather than raising: one malformed name should cost that
    inbox its IMAP and NNTP presence, not take out config generation for
    every other subsystem alongside it.
    """
    ngname = f'{NEWSGROUP_PREFIX}.{key}.{suffix}'.lower()
    bad = NEWSGROUP_INVALID_CHARS.search(ngname)
    if bad:
        logger.warning(
            'Not setting a newsgroup for %s-%s: %r is not a valid '
            'newsgroup name (character %r is not allowed); the archive '
            'stays on the web but will not appear over IMAP or NNTP',
            key,
            suffix,
            ngname,
            bad.group(),
        )
        return None
    if any(part == '' for part in ngname.split('.')):
        logger.warning(
            'Not setting a newsgroup for %s-%s: %r has an empty '
            'dot-separated component; the archive stays on the web but '
            'will not appear over IMAP or NNTP',
            key,
            suffix,
            ngname,
        )
        return None
    if NEWSGROUP_NUMERIC_TAIL.search(ngname):
        logger.warning(
            'Not setting a newsgroup for %s-%s: %r ends in a numeric '
            'component, which public-inbox reserves for mailbox slices; '
            'the archive stays on the web but will not appear over IMAP '
            'or NNTP',
            key,
            suffix,
            ngname,
        )
        return None
    return ngname


# An index this can't finish in an hour is one something is wrong with,
# and a background thread stuck in subprocess.run() forever would leave the
# setup run showing "running" with nothing behind it.
EXTINDEX_TIMEOUT = 3600


def build_extindex() -> bool:
    """Build the cross-inbox index behind /all/, and report whether it worked.

    Not an optimisation, and not something that can wait for the pull loop.
    PublicInbox::WWW opens the extindex to build its front page, so a config
    declaring `extindex "all"' -- which the one above always does -- with no
    index behind it makes /lore/ answer 500. It serves individual inboxes
    perfectly well in that state, which is what makes the failure look like
    a mystery rather than a missing index.

    So the index is built here, where the config that declares it is
    written, and setup cannot end with the archive link on the last screen
    leading to an error. Cheap when there is nothing new, and correct even
    with no inboxes at all: --all across an empty config still produces a
    valid, openable index.

    Follows kgl-pull-loop.sh's stamp protocol rather than inventing one, so
    the two agree on what has been indexed and the loop doesn't immediately
    redo this work: the stamp is dated before the run starts, and promoted
    only if it succeeds.
    """
    stamp_new = EXTINDEX_STAMP_PATH.with_name(EXTINDEX_STAMP_PATH.name + '.new')
    try:
        stamp_new.parent.mkdir(parents=True, exist_ok=True)
        stamp_new.touch()
        proc = subprocess.run(
            ['public-inbox-extindex', str(PUBLICINBOX_EXTINDEX_PATH), '--all'],
            capture_output=True,
            text=True,
            timeout=EXTINDEX_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logger.error('public-inbox-extindex timed out after %ds', EXTINDEX_TIMEOUT)
        stamp_new.unlink(missing_ok=True)
        return False
    except OSError as e:
        logger.error('Could not run public-inbox-extindex: %s', e)
        stamp_new.unlink(missing_ok=True)
        return False

    if proc.returncode != 0:
        # Left for the pull loop to retry: a failed index is a degraded
        # front page, not a reason to fail the subsystems that did import.
        logger.error(
            'public-inbox-extindex failed (exit %d): %s', proc.returncode, (proc.stderr or proc.stdout).strip()
        )
        stamp_new.unlink(missing_ok=True)
        return False

    stamp_new.replace(EXTINDEX_STAMP_PATH)
    logger.info('Built the extindex at %s', PUBLICINBOX_EXTINDEX_PATH)
    return True


# Starting lei-daemon is the slow part; the command itself only writes one
# config key.
LEI_TIMEOUT = 120


def register_lei_external() -> bool:
    """Make the extindex searchable with `lei q', and report whether it worked.

    Without this, `lei q' searches nothing local: lei only looks at the
    externals listed in its own config, and knowing where the archives are
    on disk is not the same as being told to search them. Registering the
    extindex rather than each inbox is what makes one query span every
    tracked subsystem, and keeps that true as subsystems come and go --
    the set of inboxes behind /all/ changes, the external does not.

    Registration lives here, right after the index is built, for the same
    reason the build does: `lei add-external' insists the directory exist,
    and an empty one would be registered as an external that answers
    nothing. It is safely repeated -- lei stores it as a single
    `external.$location.boost' key and writes nothing when the value is
    unchanged -- so every setup run confirms it rather than assuming an
    earlier run got there.

    The config it writes lands in $XDG_CONFIG_HOME/lei/config, which the
    Containerfile points at the volume, so this survives replacing the
    container and only has to be redone if the volume goes.

    A failure is logged and swallowed: searching from the command line is
    not what setup exists to deliver, and the web archive that is stays
    perfectly usable without it.
    """
    try:
        proc = subprocess.run(
            ['lei', 'add-external', str(PUBLICINBOX_EXTINDEX_PATH)], capture_output=True, text=True, timeout=LEI_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        logger.error('lei add-external timed out after %ds', LEI_TIMEOUT)
        return False
    except OSError as e:
        logger.error('Could not run lei add-external: %s', e)
        return False

    if proc.returncode != 0:
        logger.error('lei add-external failed (exit %d): %s', proc.returncode, (proc.stderr or proc.stdout).strip())
        return False

    logger.info('Registered %s as a lei external', PUBLICINBOX_EXTINDEX_PATH)
    return True


def is_mirrored_repo(path: Path) -> bool:
    """Is this a bare git repository grok-pull has actually populated?

    The same test public-inbox uses when it scans a cgitrc
    (PublicInbox::Config::is_git_dir), so a repo we link as a coderepo is
    one it will agree exists. grok-pull creates the directory before it
    has finished cloning, which is why existence alone is not enough.
    """
    return (path / 'objects').is_dir() and (path / 'HEAD').is_file()


def coderepo_nicks(name: str) -> List[str]:
    """Coderepo nicknames to link to one subsystem's inboxes.

    A coderepo is what lets public-inbox turn an emailed patch into
    something browsable. PublicInbox::SolverGit finds a patch's
    post-image blob in the archive, walks back to a pre-image blob that
    exists in a real repository, and applies the patch stack forward --
    so a blob that was only ever posted to a list becomes readable. With
    no repository to anchor on it cannot start at all: `lei blob' on a
    container with nothing mirrored answers "no --git-dir to try".

    Nothing has to be declared for this. The cgitrc we already write uses
    `scan-path', which makes public-inbox register every mirrored repo as
    a coderepo named by its path relative to the toplevel (Config.pm's
    cgit_repo_merge) -- that is the manifest path without its leading
    slash. All that is missing is the link, which is what
    publicinbox.<name>.coderepo is.

    The subsystem's own T: trees come first and mainline last: a patch
    sent for review is based on whatever the subsystem is carrying, so
    that tree is likeliest to hold the pre-image, and mainline is the
    fallback every subsystem shares. Repos that are not mirrored yet are
    skipped rather than linked and broken -- the next write picks them up
    once grok-pull has them.
    """
    paths: List[str] = []
    entry = SUBSYSTEMS.get(name)
    if entry:
        for tree in entry.trees:
            path = tree_manifest_path(tree)
            if path and path not in paths:
                paths.append(path)
    if LINUX_PATH not in paths:
        paths.append(LINUX_PATH)

    return [nick for nick in (path.lstrip('/') for path in paths) if is_mirrored_repo(GROKMIRROR_TOPLEVEL / nick)]


def existing_inboxes(names: List[str]) -> List[Tuple[str, str, str, Path]]:
    """(section, key, suffix, inboxdir) for each archive that exists on disk.

    korgalore creates an inbox the first time it delivers mail into it, so
    a tracked subsystem with nothing imported yet has no directory and
    must not be declared: public-inbox treats a configured inbox with no
    inboxdir as an error, not as an empty one.

    Shared by the config writer and the coderepo sync loop so the two
    cannot disagree about which inboxes exist.
    """
    lei_base_path = get_xdg_data_dir() / 'lei'
    found: List[Tuple[str, str, str, Path]] = []
    for name in names:
        if name not in SUBSYSTEMS:
            continue
        key = normalize_subsystem_name(name)
        for suffix in ('mailinglist', 'patches'):
            inboxdir = lei_base_path / f'{key}-{suffix}'
            if inboxdir.exists():
                found.append((f'{key}-{suffix}', key, suffix, inboxdir))
    return found


def planned_coderepos(names: List[str]) -> Dict[str, List[str]]:
    """The coderepo links the config should carry, keyed by section header.

    Keyed by header rather than by inbox name because the extindex takes
    coderepo lines too (PublicInbox::Config's _fill_ei reads them exactly
    as _fill_ibx does), and /all/ is the endpoint worth having them on:
    b4.midmask points there, so an agent holding nothing but a blob OID
    can ask /lore/all/<oid>/s/ without first working out which subsystem
    the patch belongs to. It gets the union of every subsystem's repos.
    """
    coderepos: Dict[str, List[str]] = {}
    all_nicks: List[str] = []
    per_name: Dict[str, List[str]] = {}
    for section, _key, _suffix, _dir in existing_inboxes(names):
        name = section.rsplit('-', 1)[0]
        if name not in per_name:
            # One resolution per subsystem, not per inbox: the two inboxes
            # of a subsystem always anchor on the same trees.
            per_name[name] = next(
                (coderepo_nicks(n) for n in names if normalize_subsystem_name(n) == name),
                [],
            )
        nicks = per_name[name]
        if nicks:
            coderepos[f'publicinbox "{section}"'] = nicks
        for nick in nicks:
            if nick not in all_nicks:
                all_nicks.append(nick)
    if all_nicks:
        coderepos['extindex "all"'] = all_nicks
    return coderepos


def configured_coderepos() -> Dict[str, List[str]]:
    """The coderepo links the config carries now, keyed the same way.

    Read with git-config rather than configparser because these are
    repeated keys and configparser keeps only the last value of one --
    which is also why configured_inboxes can get away with strict=True
    being off and this cannot. Reading them the way public-inbox reads
    them is the point: if git-config cannot see a link, neither can
    PublicInbox::Config.
    """
    try:
        proc = subprocess.run(
            ['git', 'config', '-f', str(PUBLICINBOX_CONFIG_PATH), '-z', '--get-regexp', r'\.coderepo$'],
            capture_output=True,
            text=True,
            timeout=CONFIG_READ_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning('Could not read coderepo links from %s: %s', PUBLICINBOX_CONFIG_PATH, e)
        return {}
    # 1 is "nothing matched", which is the ordinary state before anything
    # has been mirrored; anything else means the read itself failed.
    if proc.returncode == 1:
        return {}
    if proc.returncode != 0:
        logger.warning('git config failed on %s: %s', PUBLICINBOX_CONFIG_PATH, proc.stderr.strip())
        return {}

    links: Dict[str, List[str]] = {}
    for record in proc.stdout.split('\0'):
        if not record:
            continue
        key, _, value = record.partition('\n')
        kind, _, rest = key.partition('.')
        name = rest.rpartition('.')[0]
        links.setdefault(f'{kind} "{name}"', []).append(value)
    return links


def write_publicinbox_config(names: List[str]) -> None:
    """Write public-inbox's config from the current subsystem selection.

    Rebuilt from scratch each time, same as write_grokmirror_config -- keys
    are derived the exact same way `kgl track-subsystem` names its lei v2
    archives (normalize_subsystem_name + '-mailinglist'/'-patches'), so
    inboxdir always points at a real archive instead of a guessed path.
    Archives that don't exist yet (query produced no matching mail, or
    track-subsystem hasn't run for this name) are silently skipped -- the
    next call after they appear will pick them up.

    Each inbox also gets a `newsgroup' key, without which public-inbox's
    IMAP and NNTP daemons skip it entirely -- see newsgroup_name above.

    Each inbox that has a mirrored repository to anchor on also gets
    `coderepo' lines (see coderepo_nicks), which is what turns emailed
    patches into browsable blobs and makes diff hunk headers link to the
    files they touch. The extindex gets the union of them, so /all/ can
    resolve a blob without being told which subsystem it came from.

    Also writes the special-cased `[extindex "all"]` section that backs the
    /all/ endpoint (see PublicInbox::Config's ALL()/lookup_ei), a bare
    `[publicinbox] wwwlisting = all` plus `nameIsUrl = true` so WWW's front
    page lists every inbox and links it relatively, and the
    `cgitrc`/`cgitbin` directives PublicInbox::Cgit needs to serve
    /cgit under the same process (see router.psgi).
    """
    PUBLICINBOX_DIR.mkdir(parents=True, exist_ok=True)

    config = configparser.ConfigParser()
    config['publicinbox'] = {
        'wwwlisting': 'all',
        # The other half of writing no url= (see the note above). WWW's
        # front page runs every inbox past PublicInbox::WwwListing's
        # hide_inbox, which matches the wwwlisting pattern against the
        # inbox's url= list -- and an inbox with no url= has an empty
        # list, so nothing matches and it is hidden. Dropping url= on its
        # own therefore empties /lore/ instead of making its links
        # relative. nameIsUrl hands hide_inbox a stand-in `.' to match,
        # which both keeps every inbox listed and keeps its href the
        # relative `name/' we were after.
        'nameIsUrl': 'true',
        'cgitrc': CGITRC_PATH,
        'cgitbin': CGIT_BIN,
        # Emitted verbatim into every page's <head>, so the archive is
        # legible on a phone instead of rendering at desktop width and
        # being zoomed out.
        #
        # Inert for now: publicinbox.htmlhead landed upstream in 1fa2ef3f
        # (2026-03-02) and isn't in a tagged release yet, so the 2.1.0 we
        # install ignores the key entirely. lore.kernel.org gets away with
        # it because it runs a source build. Harmless to carry until the
        # package catches up, at which point it starts working on its own.
        #
        # The backslashes are required: git-config treats a bare " as
        # quoting syntax and strips it, which would collapse this to
        # `content=width=device-width, initial-scale=1' -- HTML then reads
        # the unquoted value as ending at the space, leaving initial-scale
        # as a stray attribute of its own. lore.kernel.org currently serves
        # exactly that mangled form.
        'htmlhead': r'<meta name=\"viewport\" '
        r'content=\"width=device-width, initial-scale=1\">',
    }

    inbox_count = 0
    coderepos = planned_coderepos(names)
    for section, key, suffix, inboxdir in existing_inboxes(names):
        entry = {'inboxdir': str(inboxdir)}
        ngname = newsgroup_name(key, suffix)
        if ngname:
            entry['newsgroup'] = ngname
        config[f'publicinbox "{section}"'] = entry
        inbox_count += 1

    config['extindex "all"'] = {'topdir': str(PUBLICINBOX_EXTINDEX_PATH)}

    # public-inbox labels the extindex "$EXTINDEX_DIR/description missing"
    # until this exists (PublicInbox::ExtSearch), and nothing else creates
    # it -- public-inbox-extindex only builds the index itself. Written
    # unconditionally so it reappears if the extindex is ever rebuilt from
    # scratch; korgalore writes the matching per-inbox descriptions.
    PUBLICINBOX_EXTINDEX_PATH.mkdir(parents=True, exist_ok=True)
    (PUBLICINBOX_EXTINDEX_PATH / 'description').write_text('All tracked subsystems\n', encoding='utf-8')

    with PUBLICINBOX_CONFIG_PATH.open('w') as f:
        config.write(f)
        # Same repeated-key problem as the css lines below, for the same
        # reason: coderepo is an M:N mapping, so one inbox can name
        # several. git-config accumulates a repeated section into the one
        # written above, so these stanzas add to the sections already
        # written rather than replacing them.
        for header, nicks in coderepos.items():
            f.write(f'\n[{header}]\n')
            for nick in nicks:
                f.write(f'\tcoderepo = {nick}\n')

        # configparser can't emit repeated keys within one section, and
        # publicinbox.css needs exactly that (git-config allows repeated
        # keys; a second [publicinbox] section further down accumulates
        # into the same list PublicInbox::Config reads, same as if these
        # lines had been interleaved with the ones above) -- so these are
        # appended by hand.
        #
        # These mirror what lore.kernel.org itself runs (the korglore role
        # in the infra ansible tree), not the looser example in
        # public-inbox's contrib/css/README, on two counts:
        #
        # No `title=' attribute. A titled <link rel=stylesheet> is a *named
        # stylesheet set*, and browsers enable only the first titled set
        # and disable every other one -- so titles here would ship both
        # sheets to the browser with neither of them applied.
        #
        # Light first as the base, then dark layered over it, and dark's
        # query joined with `and' rather than a comma: a comma is a media
        # query list (an OR), so `screen,(prefers-color-scheme:dark)' would
        # match every screen and apply the dark sheet unconditionally.
        #
        # The double-inside-single quoting is load-bearing where the query
        # contains spaces: git-config strips the double quotes (so they
        # protect the spaces at that layer) and leaves the single quotes
        # for public-inbox's own attribute parser.
        f.write('\n[publicinbox]\n')
        f.write(f'\tcss = {PUBLICINBOX_CSS_DIR}/216light.css media=\'"screen,print"\'\n')
        f.write(f'\tcss = {PUBLICINBOX_CSS_DIR}/216dark.css media=\'"screen and (prefers-color-scheme:dark)"\'\n')
    logger.info('Wrote public-inbox config for %d inbox(es) to %s', inbox_count, PUBLICINBOX_CONFIG_PATH)

    # Must follow the write: extindex --all reads the config to find the
    # inboxes it spans, and lei is only pointed at an index that exists.
    if build_extindex():
        register_lei_external()


# Long enough that the stat-and-read costs nothing, short enough that a
# maintainer watching the mirror finish sees the links appear rather than
# wondering whether they were meant to.
CODEREPO_SYNC_INTERVAL = 60


def sync_coderepo_links() -> bool:
    """Rewrite the config if the coderepo links it carries are out of date.

    grok-pull finishing a clone is the event that makes a tree linkable,
    and nothing else rewrites the config afterwards. Without this, a cold
    start leaves a subsystem anchored on mainline alone forever: the
    config is written while the subsystem's own tree is still cloning,
    is_mirrored_repo rightly refuses a half-cloned repo, and the next
    write only happens if the maintainer edits their selection. That is
    precisely the tree most worth having, since a posted patch is based
    on what the subsystem carries.

    Compares against what is really in the file rather than remembering
    what was written, so a container that restarts mid-clone still
    reconciles. Writing is left to write_publicinbox_config -- serve-web.sh
    watches the file and answers a change with SIGHUP, so the running
    daemon picks the links up without dropping a connection.
    """
    if not PUBLICINBOX_CONFIG_PATH.exists():
        return False
    names = read_selection()
    wanted = planned_coderepos(names)
    if wanted == configured_coderepos():
        return False

    logger.info('Mirrored repositories changed, relinking coderepos in %s', PUBLICINBOX_CONFIG_PATH)
    write_publicinbox_config(names)
    return True


def coderepo_loop() -> None:
    """Keep the coderepo links in step with what is mirrored.

    Its own loop rather than a pass in refresh_loop: that one is paced by
    how often the MAINTAINERS file and the grokmirror manifest are worth
    re-fetching, which is a day, and a day is not a sensible time to wait
    for a link to a tree that finished cloning an hour ago.
    """
    while True:
        time.sleep(CODEREPO_SYNC_INTERVAL)
        try:
            sync_coderepo_links()
        except Exception as e:
            # Same posture as refresh_loop: one bad pass is not worth
            # losing the loop over, and the next one retries.
            logger.warning('Could not sync coderepo links: %s', e)


# Mirroring is a handoff, not a job this app runs and waits on. A first
# mirror of the kernel tree takes far longer than a maintainer should sit
# at one screen for, and it never really ends afterwards -- grok-pull -o
# keeps re-checking the manifest for the life of the container. So
# /api/grokmirror/pull writes the config and returns; grok-pull-loop.sh
# notices and (re)starts grok-pull; and /api/grokmirror/status reports on
# what that process is doing.
#
# Everything the status reports is read off the volume rather than held in
# this process, which is what lets it outlive a page reload, a lost
# connection and a container restart alike. There is no 'done' in it: the
# useful question is whether the mirror is under way and making progress,
# not whether some run the dashboard started has ended.


def _grok_pull_running() -> bool:
    """Whether grok-pull-loop.sh currently has a grok-pull going.

    The pid file is the loop's, and an unclean exit (a SIGKILL, a container
    that died) leaves a stale one behind, so the process is confirmed
    through /proc rather than taken on the file's word.
    """
    try:
        pid = int(GROKMIRROR_PID_PATH.read_text().strip())
        cmdline = Path(f'/proc/{pid}/cmdline').read_bytes()
    except (OSError, ValueError):
        return False
    return b'grok-pull' in cmdline


def mirror_progress(paths: List[str]) -> List[Dict[str, Any]]:
    """Per-repo mirror state, in the order the maintainer picked them.

    grokmirror's local manifest is the record of what it has actually
    finished: a repo appears there once its clone is complete, so this says
    "3 of 5 mirrored" without having to parse a word of grok-pull's output.
    """
    try:
        with gzip.open(GROKMIRROR_MANIFEST_PATH, 'rt') as f:
            mirrored = json.load(f)
    except (OSError, ValueError):
        mirrored = {}
    return [{'path': path, 'mirrored': path in mirrored} for path in paths]


def mirror_status(since: int) -> Dict[str, Any]:
    """Snapshot of the mirror for /api/grokmirror/status polling.

    `since' is a byte offset into grok-pull's log, not a line number: the
    log is the durable record here, so a page that reloads picks up exactly
    where it left off instead of starting from whatever this process still
    had in memory. A log shorter than `since' was rotated or truncated
    underneath us, so it's read from the top again rather than seeked past
    the end.
    """
    selected = read_repo_selection()
    if not GROKMIRROR_CONF_PATH.exists():
        state = 'idle'
    elif _grok_pull_running():
        state = 'running'
    else:
        # The config is there but no grok-pull is. Nearly always this is
        # just the second or two before grok-pull-loop.sh comes around;
        # 'starting' says so without claiming more than we know.
        state = 'starting'

    text, offset = read_mirror_log(since)
    progress = mirror_progress(selected)
    return {
        'state': state,
        'repos': progress,
        'mirrored': sum(1 for repo in progress if repo['mirrored']),
        'total': len(progress),
        'lines': text.splitlines(),
        'next': offset,
    }


def read_mirror_log(since: int) -> Tuple[str, int]:
    """Read grok-pull's log from byte offset *since*, returning (text, next).

    Only whole lines are handed back: grok-pull may well be part-way
    through writing one, and `next' stays put on the partial remainder so
    the next poll returns it complete rather than split across two.
    """
    try:
        size = GROKMIRROR_LOG_PATH.stat().st_size
    except OSError:
        return '', 0

    start = 0 if since > size else max(since, size - MAX_MIRROR_LOG_BYTES)
    try:
        with GROKMIRROR_LOG_PATH.open('rb') as f:
            f.seek(start)
            raw = f.read()
    except OSError:
        return '', since

    tail = raw.rfind(b'\n')
    if tail < 0:
        return '', start
    return raw[: tail + 1].decode('utf-8', 'replace'), start + tail + 1


# "Sync now", the dashboard's one button for "get me what upstream has,
# without waiting for the next interval". Neither half runs anything here.
# kgl-pull-loop.sh is the only thing that pulls mail, because a pull it
# didn't run would also skip the extindex and the pull stamp that come
# after it. grok-pull-loop.sh is the only thing that may start grok-pull
# (its header says why). So this app only asks, and reports what it can
# see from the volume.


def _read_int(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def read_pull_state() -> Dict[str, int]:
    """What kgl-pull-loop.sh last wrote about its passes.

    `started' on its own means a pass is under way; `finished' and `ok'
    are added when it ends. Lines that don't parse are skipped rather than
    failing the whole read, so a newer loop that writes more keys doesn't
    break an older dashboard.
    """
    try:
        text = KGL_PULL_STATE_PATH.read_text()
    except OSError:
        return {}
    state: Dict[str, int] = {}
    for line in text.splitlines():
        key, sep, value = line.partition('=')
        if sep and value.strip().isdigit():
            state[key.strip()] = int(value)
    return state


def mail_sync_status() -> Dict[str, Any]:
    """Where the mail side of a sync request is, for the dashboard to poll.

    The rules are kgl-pull-loop.sh's, read from the other end. A request
    is answered by the first pass that starts in a later second, so it is
    `pending' until one has. While a pass has started but not finished it
    is `running'. A container that died part-way through a pass leaves
    `running' behind, but only until the next start, which pulls straight
    away.

    Seconds, not the float mtime, because `stat -c %Y' is all the loop
    compares, and the two must agree on whether a request came before or
    after a pass.
    """
    try:
        requested: Optional[int] = int(SYNC_REQUEST_PATH.stat().st_mtime)
    except OSError:
        requested = None
    state = read_pull_state()
    started = state.get('started')
    finished = state.get('finished')
    return {
        'tracked': bool(read_selection()),
        'requested': requested,
        'pending': requested is not None and (started is None or requested >= started),
        'running': started is not None and finished is None,
        'started': started,
        'finished': finished,
        'ok': bool(state.get('ok')) if finished is not None else None,
        # The last pass that went fully right -- the same stamp router.psgi
        # sends to b4 as `updated='.
        'updated': _read_int(PULL_STAMP_PATH),
    }


def request_mail_sync() -> Dict[str, Any]:
    """Ask kgl-pull-loop.sh for a pass now, and say where that stands.

    Touching the file is the whole request. It needs no lock and no queue.
    Two presses in a row are one request, and a press during a pass gets
    the pass after it, which is what the button promises.
    """
    if read_selection():
        SYNC_REQUEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        SYNC_REQUEST_PATH.touch()
        logger.info('Asked kgl-pull-loop.sh for a mail sync')
    return mail_sync_status()


def request_mirror_sync() -> Dict[str, Any]:
    """Ask the running grok-pull to fetch every mirrored repo now.

    grok-pull -o listens on [pull] socket for repo paths, one per line, and
    queues each one it finds in its local manifest for a fetch, whether or
    not the remote manifest says it changed. This is the interface
    grokmirror has for exactly this, so it needs no second grok-pull and no
    restart of this one.

    One connection per repo, because the listener drops the connection at
    the first path it doesn't know. Only repos already in the local
    manifest are sent. The listener would ignore the others anyway, and a
    repo that isn't there yet is still on its first clone, which is as
    current as it gets.
    """
    selected = read_repo_selection()
    if not selected:
        return {'state': 'none', 'queued': [], 'cloning': []}

    try:
        with gzip.open(GROKMIRROR_MANIFEST_PATH, 'rt') as f:
            mirrored = set(json.load(f))
    except (OSError, ValueError):
        mirrored = set()
    queued = [path for path in selected if path in mirrored]
    cloning = [path for path in selected if path not in mirrored]
    result: Dict[str, Any] = {'state': 'queued', 'queued': [], 'cloning': cloning}
    if not queued:
        return result

    try:
        is_socket = stat.S_ISSOCK(GROKMIRROR_SOCKET_PATH.stat().st_mode)
    except OSError:
        is_socket = False
    if not is_socket:
        # A grokmirror.conf from before the dashboard wrote `socket' into
        # it. The next save from the repos screen adds it.
        return {
            **result,
            'state': 'unavailable',
            'message': 'grok-pull is not listening for sync requests. '
            'Save the repo selection again (Change setup, then Start mirroring) to turn it on.',
        }

    for path in queued:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(GROKMIRROR_SOCKET_TIMEOUT)
                conn.connect(str(GROKMIRROR_SOCKET_PATH))
                conn.sendall(f'{path}\n'.encode())
        except OSError as e:
            # Refused is the usual one: grok-pull is between restarts and
            # its old socket file is still there. It fetches everything as
            # it starts anyway.
            logger.warning('Could not ask grok-pull to sync %s: %s', path, e)
            return {
                **result,
                'state': 'unavailable',
                'message': f'grok-pull did not answer ({e.strerror or e}). '
                'If it is restarting, it checks every tree when it is back.',
            }
        result['queued'].append(path)
    logger.info('Asked grok-pull to sync %d repo(s)', len(queued))
    return result


def read_daemons() -> List[str]:
    """Load which optional listeners are turned on, ignoring unknown names."""
    try:
        saved = json.loads(DAEMONS_PATH.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning('Ignoring unreadable daemon selection at %s', DAEMONS_PATH)
        return []

    return [name for name in saved.get('enabled', []) if name in DAEMON_IDS]


def write_daemons(names: List[str]) -> List[str]:
    """Save which optional listeners should be on, and return what was saved.

    serve-web.sh watches this file's mtime and restarts public-inbox-netd
    when it changes, so writing it is the whole of turning a daemon on or
    off. It is rewritten even when the set is unchanged -- an unchanged
    mtime means no restart, which is exactly right for a no-op save.
    """
    known: List[str] = [str(daemon['id']) for daemon in DAEMONS if daemon['id'] in set(names)]
    DAEMONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DAEMONS_PATH.write_text(json.dumps({'enabled': known}, indent=2) + '\n')
    logger.info('Saved %d enabled daemon(s) to %s', len(known), DAEMONS_PATH)
    return known


def daemons_status() -> Dict[str, Any]:
    """What the daemons screen shows: each listener, its port, and its state."""
    enabled = set(read_daemons())
    return {
        'daemons': [
            {
                **daemon,
                'enabled': daemon['id'] in enabled,
                'publish': f'-p {PUBLISH_ADDRESS}:{daemon["port"]}:{daemon["port"]}',
            }
            for daemon in DAEMONS
        ],
    }


# What the finished container looks like from outside it. This backs the
# last setup screen, which doubles as the front page of a container that is
# already set up -- so it has to describe what is *actually* being served,
# not what the selection implies. The inbox list is read back out of the
# generated public-inbox config rather than recomputed from the selection:
# an archive only lands in there once it exists on disk, and the newsgroup
# names IMAP and NNTP advertise are written there too.


def configured_inboxes() -> List[Dict[str, Any]]:
    """The inboxes public-inbox is really serving, read from its own config.

    strict=False because the file deliberately carries two [publicinbox]
    sections (see write_publicinbox_config) -- git-config accumulates them,
    configparser would otherwise refuse to reread its own output.
    """
    config = configparser.ConfigParser(strict=False)
    try:
        config.read(PUBLICINBOX_CONFIG_PATH)
    except (OSError, configparser.Error):
        logger.warning('Could not read %s', PUBLICINBOX_CONFIG_PATH)
        return []

    inboxes = []
    for section in config.sections():
        match = re.fullmatch(r'publicinbox "(.+)"', section)
        if not match:
            continue
        name = match.group(1)
        inboxes.append(
            {
                'name': name,
                'url': config[section].get('url', f'{ROUTER_PUBLIC_BASE}/lore/{name}/'),
                # Absent only if newsgroup_name rejected it, in which case IMAP
                # and NNTP skip the inbox and the screen should not claim a
                # folder that isn't there.
                'newsgroup': config[section].get('newsgroup'),
            }
        )
    return sorted(inboxes, key=lambda inbox: inbox['name'])


def summary() -> Dict[str, Any]:
    """Everything the finished-setup screen shows.

    `container' is podman's doing: it sets a container's hostname to the
    short container ID, not to --name, so this is exactly the argument
    `podman exec' wants. The name the maintainer chose isn't visible from
    in here at all.
    """
    repos = read_repo_selection()
    inboxes = configured_inboxes()
    return {
        # Whether this container has been set up at all -- decides whether
        # the browser lands here or on the subsystem picker.
        'configured': bool(read_selection() or repos),
        # Who this container was set up for. Read fresh rather than passed
        # through from the identity screen, so a summary opened on a later
        # visit shows what is on the volume now.
        'identity': read_identity(),
        'base': ROUTER_PUBLIC_BASE,
        'lore': f'{ROUTER_PUBLIC_BASE}/lore/',
        'cgit': f'{ROUTER_PUBLIC_BASE}/cgit/',
        'upstream': GROKMIRROR_SITE,
        'inboxes': inboxes,
        'repos': [
            {
                'path': path,
                'clone': f'{ROUTER_PUBLIC_BASE}{path}',
                'upstream': f'{GROKMIRROR_SITE}{path}',
            }
            for path in repos
        ],
        # midmask is where b4 *fetches* messages, so it points at the local
        # mirror. linkmask is what ends up in Link: trailers of published
        # commits, so it stays public -- see the screen's own warning.
        'b4': {
            'midmask': f'{ROUTER_PUBLIC_BASE}/lore/all/%s',
            'linkmask': 'https://patch.msgid.link/%s',
        },
        # The MCP server listens on its own port inside the container and
        # is reached through the router, so this is the one address worth
        # showing -- MCP_PORT is not published and saying so would only
        # invite somebody to publish it.
        'mcp': {'url': f'{ROUTER_PUBLIC_BASE}/mcp'},
        'daemons': daemons_status()['daemons'],
        'container': socket.gethostname(),
        # For the "get a shell in here" block: the files worth editing by
        # hand, and which are watched for changes. Read from the same env
        # the loop scripts read, so the screen can't drift from them.
        'paths': {
            'publicinbox': str(PUBLICINBOX_CONFIG_PATH),
            'grokmirror': str(GROKMIRROR_CONF_PATH),
            'korgalore': str(get_xdg_config_dir() / 'korgalore.toml'),
        },
    }


class SetupHandler(BaseHTTPRequestHandler):
    server_version = 'maintainer-setup'
    # The UI polls the two status endpoints once a second while a run is
    # going, and the base class defaults to HTTP/1.0 -- a new connection per
    # poll, plus a fresh one through the router's proxy each time.
    protocol_version = 'HTTP/1.1'

    def await_ready(self, event: threading.Event, what: str) -> bool:
        """Block until a background-loaded table is usable.

        Each request gets its own thread (ThreadingHTTPServer), so waiting
        here costs nothing but this one client's time, and only on the rare
        request that arrives during the first few seconds of a cold start.
        """
        if event.wait(READY_TIMEOUT):
            return True
        self.send_error(503, f'Still loading {what} -- try again shortly')
        return False

    def do_GET(self) -> None:
        route = urlparse(self.path)

        if route.path == '/':
            self.send_bytes(INDEX_PATH.read_bytes(), 'text/html; charset=utf-8')
        elif route.path == '/api/subsystems':
            if not self.await_ready(SUBSYSTEMS_READY, 'the subsystem list'):
                return
            query = parse_qs(route.query).get('q', [''])[0]
            self.send_json({'results': search(query), 'limit': MAX_RESULTS})
        elif route.path == '/api/selection':
            if not self.await_ready(SUBSYSTEMS_READY, 'the subsystem list'):
                return
            self.send_json({'selected': read_selection()})
        elif route.path == '/api/identity':
            # Needs the table for the same reason the selection does: the
            # matches are read out of it, and answering from an empty one
            # would say "nothing lists you" to somebody who maintains six
            # subsystems.
            if not self.await_ready(SUBSYSTEMS_READY, 'the subsystem list'):
                return
            self.send_json(identity_state())
        elif route.path == '/api/repos':
            if not self.await_ready(MANIFEST_READY, 'the repository list'):
                return
            query = parse_qs(route.query).get('q', [''])[0]
            self.send_json({'results': search_repos(query), 'limit': MAX_RESULTS})
        elif route.path == '/api/repos/suggested':
            if not self.await_ready(MANIFEST_READY, 'the repository list'):
                return
            self.send_json({'results': suggested_repos()})
        elif route.path == '/api/repo-selection':
            if not self.await_ready(MANIFEST_READY, 'the repository list'):
                return
            self.send_json({'selected': read_repo_selection()})
        elif route.path == '/api/generate/status':
            try:
                since = int(parse_qs(route.query).get('since', ['0'])[0])
            except ValueError:
                self.send_error(400, 'Expected an integer "since"')
                return
            self.send_json(generate_status(since))
        elif route.path == '/api/grokmirror/config-check':
            # On demand rather than on a timer: the config only changes
            # when somebody changes it, and the online variant reaches out
            # to the remote site, which is not something to do on a poll.
            if not GROKMIRROR_CONF_PATH.exists():
                self.send_json({'configured': False, 'report': None})
                return
            online = parse_qs(route.query).get('online', ['0'])[0] == '1'
            self.send_json(
                {
                    'configured': True,
                    'report': check_grokmirror_config(online=online),
                }
            )
        elif route.path == '/api/grokmirror/status':
            try:
                since = int(parse_qs(route.query).get('since', ['0'])[0])
            except ValueError:
                self.send_error(400, 'Expected an integer "since"')
                return
            self.send_json(mirror_status(since))
        elif route.path == '/api/daemons':
            self.send_json(daemons_status())
        elif route.path == '/api/summary':
            self.send_json(summary())
        elif route.path == '/api/sync':
            self.send_json({'mail': mail_sync_status()})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route not in (
            '/api/identity',
            '/api/selection',
            '/api/generate',
            '/api/repo-selection',
            '/api/grokmirror/pull',
            '/api/daemons',
            '/api/sync',
        ):
            self.send_error(404)
            return

        # Same tables, same wait as on the GET side -- these writes filter
        # what they're given against SUBSYSTEMS/MANIFEST, so running one
        # before the table is loaded would silently save an empty selection.
        if route in ('/api/identity', '/api/selection', '/api/generate'):
            if not self.await_ready(SUBSYSTEMS_READY, 'the subsystem list'):
                return
        elif route == '/api/repo-selection':
            if not self.await_ready(MANIFEST_READY, 'the repository list'):
                return

        length = int(self.headers.get('Content-Length', '0'))
        if length > MAX_BODY:
            self.send_error(413)
            return
        body = self.rfile.read(length)

        if route == '/api/identity':
            try:
                payload = json.loads(body)
                raw = str(payload['address'])
            except (ValueError, KeyError, TypeError):
                self.send_error(400, 'Expected {"address": "..."}')
                return
            try:
                write_identity(raw)
            except ValueError as e:
                # 422 rather than 400: the request was well formed, the
                # address in it wasn't, and the screen shows the difference
                # as a message next to the box instead of a failed save.
                self.send_error(422, str(e))
                return
            self.send_json(identity_state())
            return

        if route == '/api/selection':
            try:
                payload = json.loads(body)
                names = list(payload['selected'])
            except (ValueError, KeyError, TypeError):
                self.send_error(400, 'Expected {"selected": [...]}')
                return
            self.send_json({'selected': write_selection(names)})
            return

        if route == '/api/repo-selection':
            try:
                payload = json.loads(body)
                paths = list(payload['selected'])
            except (ValueError, KeyError, TypeError):
                self.send_error(400, 'Expected {"selected": [...]}')
                return
            self.send_json({'selected': write_repo_selection(paths)})
            return

        if route == '/api/grokmirror/pull':
            try:
                payload = json.loads(body)
                paths = list(payload['selected'])
            except (ValueError, KeyError, TypeError):
                self.send_error(400, 'Expected {"selected": [...]}')
                return
            # Save the selection and the config together: grok-pull-loop.sh
            # keys its restart off the config's mtime, so writing it is the
            # whole of "start mirroring these". Nothing is run here, and
            # nothing is waited on -- the status endpoint reports what the
            # loop's grok-pull gets up to from here.
            known = write_repo_selection(paths)
            adopt_preloads(known)
            # The report comes back from the write itself -- handed on with
            # the response so a config error shows up next to the button
            # that caused it, not only in the container log.
            report = write_grokmirror_config(known)
            self.send_json(
                {
                    'status': mirror_status(0)['state'],
                    'config_check': report,
                }
            )
            return

        if route == '/api/sync':
            # No body to read: there is nothing to choose. It syncs whatever
            # is set up, which is what the button says.
            self.send_json({'mail': request_mail_sync(), 'git': request_mirror_sync()})
            return

        if route == '/api/daemons':
            try:
                payload = json.loads(body)
                names = list(payload['enabled'])
            except (ValueError, KeyError, TypeError):
                self.send_error(400, 'Expected {"enabled": [...]}')
                return
            write_daemons(names)
            self.send_json(daemons_status())
            return

        # /api/generate
        try:
            payload = json.loads(body)
            names = list(payload['selected'])
            since = str(payload.get('since') or '90.days.ago')
        except (ValueError, KeyError, TypeError):
            self.send_error(400, 'Expected {"selected": [...], "since": "..."}')
            return
        started = start_generate(names, since)
        self.send_json({'started': started, 'status': generate_status(0)['status']})

    def send_json(self, payload: Dict[str, Any]) -> None:
        self.send_bytes(json.dumps(payload).encode(), 'application/json')

    def do_HEAD(self) -> None:
        # liblore asks `HEAD <origin>/<msgid>/' when a thread is not on the
        # mirror, and that path has no /lore in it, so the router hands it
        # here. Without this the base class answers 501, which shows up in
        # the log as "Unsupported method ('HEAD')" every time b4 misses.
        # The answer is GET's, without the body: send_bytes leaves it out,
        # and send_error already does the same for HEAD.
        self.do_GET()

    def send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        # The real length even for HEAD, so it says what a GET would get.
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        # A HEAD answer must not carry a body. On a kept-alive HTTP/1.1
        # connection the client would read it as the start of the next
        # response.
        if self.command != 'HEAD':
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        logger.info('%s %s', self.address_string(), format % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s: %(message)s')

    # Loads happen off this thread so the port is bound (and the page
    # served) right away; see SUBSYSTEMS_READY.
    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=coderepo_loop, daemon=True).start()

    server = ThreadingHTTPServer((HOST, PORT), SetupHandler)
    logger.info('Dashboard listening on http://%s:%d/', HOST, PORT)
    server.serve_forever()


if __name__ == '__main__':
    main()
