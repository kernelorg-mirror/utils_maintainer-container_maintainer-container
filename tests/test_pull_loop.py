# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
"""Tests for the pull stamp, kgl-pull-loop.sh and lore_coverage.py, and
for how the loop answers the dashboard's "Sync now".

The stamp is what router.psgi sends as `updated=' in X-Archive-Coverage,
and a client that sees it trusts the mirror's "nothing new" without asking
upstream. A stamp that moves when it shouldn't makes clients miss mail, so
most of these tests are about the ways a pass can go wrong and still look
fine.

The loop runs for real, one pass at a time (`--once'), with stand-ins for
kgl and public-inbox-extindex on PATH. The coverage guard runs for real
too, on korgalore config written by korgalore's own generate_subsystem_config,
so it reads the same files `kgl track-subsystem' writes.
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import lore_coverage
import pytest
from korgalore.maintainers import generate_subsystem_config

REPO = Path(__file__).resolve().parent.parent
LOOP = REPO / 'kgl-pull-loop.sh'

SUBSYSTEMS = ('spi', 'usb')


class Volume:
    """A data volume with two tracked subsystems, both served and pulled."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.korgalore_conf = root / 'korgalore' / 'korgalore.toml'
        self.conf_d = root / 'korgalore' / 'conf.d'
        self.lei = root / 'lei'
        self.pi_config = root / 'publicinbox' / 'config'
        self.stamp = root / 'lore-updated'
        self.extindex_stamp = root / 'publicinbox' / '.extindex-stamp'
        self.bin = root / 'bin'
        self.log = root / 'calls.log'
        self.state = root / 'kgl-pull-state'
        self.sync_request = root / 'sync-requested'

        self.conf_d.mkdir(parents=True)
        self.pi_config.parent.mkdir(parents=True)
        self.bin.mkdir()
        self.korgalore_conf.write_text("[targets.local]\ntype = 'pipe'\ncommand = 'true'\n", encoding='utf-8')
        for key in SUBSYSTEMS:
            self.track(key)
        self.serve(*(f'{key}-mailinglist' for key in SUBSYSTEMS))

    def track(self, key: str) -> None:
        """What `kgl track-subsystem' leaves behind for one subsystem."""
        toml = generate_subsystem_config(key, 'local', [], self.lei, '2026-01-01', key.upper())
        (self.conf_d / f'{key}.toml').write_text(toml, encoding='utf-8')
        for suffix in ('mailinglist', 'patches'):
            (self.lei / f'{key}-{suffix}' / 'git').mkdir(parents=True, exist_ok=True)
            (self.lei / f'{key}-{suffix}' / 'git' / '0.git').write_text('mail\n', encoding='utf-8')

    def serve(self, *names: str, inboxdir: Optional[Dict[str, Path]] = None) -> None:
        """Write the public-inbox config to serve these archives, each from
        its lei archive unless inboxdir says otherwise."""
        paths = {name: self.lei / name for name in names} | (inboxdir or {})
        sections = [f'[publicinbox "{name}"]\n\tinboxdir = {paths[name]}\n' for name in names]
        self.pi_config.write_text(''.join(sections), encoding='utf-8')

    def indexed(self) -> None:
        """Make the extindex look current, so the loop skips it."""
        self.extindex_stamp.touch()
        later = time.time() + 60
        os.utime(self.extindex_stamp, (later, later))

    def calls(self) -> List[str]:
        return self.log.read_text(encoding='utf-8').splitlines() if self.log.exists() else []

    def pull_state(self) -> Dict[str, int]:
        """KGL_PULL_STATE_PATH, read the way the dashboard reads it."""
        lines = self.state.read_text(encoding='utf-8').splitlines()
        return {key: int(value) for key, _, value in (line.partition('=') for line in lines)}

    def request_sync(self, at: float) -> None:
        """What the dashboard's "Sync now" does, dated `at'."""
        self.sync_request.touch()
        os.utime(self.sync_request, (at, at))


@pytest.fixture
def volume(tmp_path: Path) -> Volume:
    return Volume(tmp_path)


# The coverage guard


def test_every_served_archive_is_pulled(volume: Volume) -> None:
    assert lore_coverage.uncovered(volume.pi_config, volume.korgalore_conf) == []


def test_a_paused_subsystem_is_not_covered(volume: Volume) -> None:
    """`kgl untrack-subsystem --pause' from a shell, behind the dashboard's
    back: still served, no longer pulled."""
    (volume.conf_d / 'spi.toml').rename(volume.conf_d / 'spi.toml.paused')

    assert lore_coverage.uncovered(volume.pi_config, volume.korgalore_conf) == ['spi-mailinglist']


def test_a_deleted_subsystem_is_not_covered(volume: Volume) -> None:
    (volume.conf_d / 'usb.toml').unlink()

    assert lore_coverage.uncovered(volume.pi_config, volume.korgalore_conf) == ['usb-mailinglist']


def test_a_feed_no_delivery_uses_is_not_covered(volume: Volume) -> None:
    """korgalore only opens the feeds some delivery points at, so a feed
    on its own is never pulled."""
    toml = (volume.conf_d / 'spi.toml').read_text(encoding='utf-8')
    (volume.conf_d / 'spi.toml').write_text(toml[: toml.index('[deliveries.')], encoding='utf-8')

    assert lore_coverage.uncovered(volume.pi_config, volume.korgalore_conf) == ['spi-mailinglist']


def test_an_archive_pulled_but_not_served_is_fine(volume: Volume) -> None:
    """A subsystem deselected on the dashboard but still in conf.d: pulling
    more than is served promises nothing wrong."""
    volume.serve('spi-mailinglist')

    assert lore_coverage.uncovered(volume.pi_config, volume.korgalore_conf) == []


def test_paths_are_compared_resolved(volume: Volume, tmp_path: Path) -> None:
    """The same archive by another path is still the same archive."""
    (tmp_path / 'elsewhere').symlink_to(volume.lei)
    volume.serve('spi-mailinglist', inboxdir={'spi-mailinglist': tmp_path / 'elsewhere' / 'spi-mailinglist'})

    assert lore_coverage.uncovered(volume.pi_config, volume.korgalore_conf) == []


def test_nothing_served_yet_is_covered(volume: Volume) -> None:
    volume.pi_config.unlink()

    assert lore_coverage.uncovered(volume.pi_config, volume.korgalore_conf) == []


def test_the_guard_names_what_it_found(volume: Volume) -> None:
    """Run as the loop runs it, so this is what lands in the container log."""
    (volume.conf_d / 'spi.toml').unlink()

    proc = subprocess.run(
        [sys.executable, str(REPO / 'setup' / 'lore_coverage.py'), str(volume.pi_config), str(volume.korgalore_conf)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 1
    assert 'spi-mailinglist' in proc.stderr
    assert 'usb-mailinglist' not in proc.stderr


def test_a_broken_korgalore_config_is_not_covered(volume: Volume) -> None:
    """kgl pull can't load it either, so nothing it says can be trusted."""
    (volume.conf_d / 'usb.toml').write_text('[feeds.usb\n', encoding='utf-8')

    assert lore_coverage.main([str(volume.pi_config), str(volume.korgalore_conf)]) == 2


def test_a_missing_korgalore_config_is_not_covered(volume: Volume) -> None:
    volume.korgalore_conf.unlink()

    assert lore_coverage.main([str(volume.pi_config), str(volume.korgalore_conf)]) == 2


# The loop


def pull_once(volume: Volume, kgl: int = 0, extindex: int = 0, kgl_sleep: float = 0, kgl_does: str = '') -> int:
    """One pass of kgl-pull-loop.sh. The stand-ins log their arguments and
    exit with the status given here; kgl_does is a shell command the fake
    kgl runs first, for whatever else happens during a pull."""
    (volume.bin / 'kgl').write_text(
        f'#!/bin/sh\n{kgl_does}\necho "kgl $*" >> {volume.log}\nsleep {kgl_sleep}\nexit {kgl}\n', encoding='utf-8'
    )
    (volume.bin / 'public-inbox-extindex').write_text(
        f'#!/bin/sh\necho "extindex $*" >> {volume.log}\nexit {extindex}\n', encoding='utf-8'
    )
    # The guard imports korgalore, so it needs this interpreter, venv and
    # all. A symlink would lose the venv: Python finds it from the path it
    # was started by.
    (volume.root / 'venv' / 'bin').mkdir(parents=True, exist_ok=True)
    (volume.root / 'venv' / 'bin' / 'python').write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n', encoding='utf-8')
    for tool in ('kgl', 'public-inbox-extindex'):
        (volume.bin / tool).chmod(0o755)
    (volume.root / 'venv' / 'bin' / 'python').chmod(0o755)

    return subprocess.run(
        [str(LOOP), '--once'], env=loop_env(volume), capture_output=True, text=True, check=False
    ).returncode


def loop_env(volume: Volume, interval: int = 600) -> Dict[str, str]:
    """The environment the loop runs in, every path pointed into volume."""
    return {
        'PATH': f'{volume.bin}:{os.environ["PATH"]}',
        'DEFAULTS_ENV': str(REPO / 'defaults.env'),
        'DATA_DIR': str(volume.root),
        'XDG_CONFIG_HOME': str(volume.root / 'xdg' / 'config'),
        'XDG_DATA_HOME': str(volume.root / 'xdg' / 'data'),
        'VENV_DIR': str(volume.root / 'venv'),
        'KGL': str(volume.bin / 'kgl'),
        'KORGALORE_CONF_PATH': str(volume.korgalore_conf),
        'LEI_ARCHIVES_PATH': str(volume.lei),
        'PI_CONFIG': str(volume.pi_config),
        'PUBLICINBOX_EXTINDEX_PATH': str(volume.root / 'publicinbox' / 'extindex'),
        'EXTINDEX_STAMP_PATH': str(volume.extindex_stamp),
        'PULL_STAMP_PATH': str(volume.stamp),
        'LORE_COVERAGE_SCRIPT': str(REPO / 'setup' / 'lore_coverage.py'),
        'KGL_PULL_STATE_PATH': str(volume.state),
        'SYNC_REQUEST_PATH': str(volume.sync_request),
        'KGL_PULL_INTERVAL': str(interval),
        'SYNC_POLL_INTERVAL': '1',
    }


def stale(volume: Volume) -> None:
    """A stamp from an earlier good pass, for a bad one to leave alone."""
    volume.stamp.write_text('1000\n', encoding='utf-8')


def test_a_clean_pass_writes_when_it_started(volume: Volume) -> None:
    """The start, not the end: mail that reached upstream while the pull
    ran may or may not be in it."""
    before = int(time.time())
    assert pull_once(volume, kgl_sleep=1.2) == 0
    after = int(time.time())

    started = int(volume.stamp.read_text(encoding='utf-8'))
    assert before <= started < after
    assert not volume.stamp.with_name('lore-updated.new').exists()


def test_the_pull_is_asked_to_report_a_failed_feed(volume: Volume) -> None:
    """Without the flag, kgl pull exits 0 even when a feed failed."""
    pull_once(volume)

    assert 'kgl pull --fail-on-feed-error' in volume.calls()


@pytest.mark.parametrize('status', [1, 2, 3], ids=['crash', 'usage-error', 'failed-feed'])
def test_a_failed_pull_leaves_the_stamp(volume: Volume, status: int) -> None:
    """Every failure counts, an unknown flag (2) included -- there is no
    telling it from a real one, and no need to."""
    stale(volume)

    assert pull_once(volume, kgl=status) != 0
    assert volume.stamp.read_text(encoding='utf-8') == '1000\n'


def test_a_failed_extindex_leaves_the_stamp(volume: Volume) -> None:
    """/lore/all/ only shows what the extindex has."""
    stale(volume)

    assert pull_once(volume, extindex=1) != 0
    assert volume.stamp.read_text(encoding='utf-8') == '1000\n'
    assert any(call.startswith('extindex') for call in volume.calls())


def test_a_skipped_extindex_still_moves_the_stamp(volume: Volume) -> None:
    """Nothing new to index means the index is current."""
    volume.indexed()
    stale(volume)

    assert pull_once(volume) == 0
    assert not any(call.startswith('extindex') for call in volume.calls())
    assert volume.stamp.read_text(encoding='utf-8') != '1000\n'


def test_an_uncovered_archive_leaves_the_stamp(volume: Volume) -> None:
    stale(volume)
    (volume.conf_d / 'spi.toml').rename(volume.conf_d / 'spi.toml.paused')

    assert pull_once(volume) != 0
    assert volume.stamp.read_text(encoding='utf-8') == '1000\n'


def test_a_subsystem_paused_during_the_pull_leaves_the_stamp(volume: Volume) -> None:
    """Paused after the pull loaded the config: the pull did update it,
    but the next one won't, and the stamp would outlive it."""
    stale(volume)
    toml = volume.conf_d / 'spi.toml'

    assert pull_once(volume, kgl_does=f'mv {toml} {toml}.paused') != 0
    assert volume.stamp.read_text(encoding='utf-8') == '1000\n'


def test_a_subsystem_resumed_during_the_pull_leaves_the_stamp(volume: Volume) -> None:
    """Resumed after the pull loaded the config: it is served now, but this
    pull never touched it. Only the check before the pull can see that."""
    stale(volume)
    toml = volume.conf_d / 'spi.toml'
    toml.rename(f'{toml}.paused')

    assert pull_once(volume, kgl_does=f'mv {toml}.paused {toml}') != 0
    assert volume.stamp.read_text(encoding='utf-8') == '1000\n'


def test_nothing_tracked_writes_no_stamp(volume: Volume) -> None:
    """No korgalore.toml yet, so no pull ran."""
    volume.korgalore_conf.unlink()

    assert pull_once(volume) != 0
    assert not volume.stamp.exists()
    assert not any(call.startswith('kgl') for call in volume.calls())


# Sync now


def test_a_pass_says_when_it_ran_and_how_it_went(volume: Volume) -> None:
    """The dashboard reads this to know the pass it asked for is done."""
    before = int(time.time())
    assert pull_once(volume) == 0
    after = int(time.time())

    state = volume.pull_state()
    assert before <= state['started'] <= state['finished'] <= after
    assert state['ok'] == 1
    assert not volume.state.with_name('kgl-pull-state.new').exists()


def test_a_failed_pass_says_so(volume: Volume) -> None:
    assert pull_once(volume, kgl=1) != 0

    assert volume.pull_state()['ok'] == 0
    assert 'finished' in volume.pull_state()


def test_a_running_pass_has_not_finished(volume: Volume) -> None:
    """While kgl runs, the state has a start and nothing else, so the
    dashboard shows the pass as under way."""
    seen = volume.root / 'state-during-pull'

    pull_once(volume, kgl_does=f'cp {volume.state} {seen}')

    assert seen.read_text(encoding='utf-8').splitlines() == [f'started={volume.pull_state()["started"]}']


def wait_after_pass(volume: Volume, started: int, interval: int) -> 'subprocess.Popen[str]':
    """Start the loop's wait after a pass that began at `started'."""
    return subprocess.Popen(
        [str(LOOP), '--wait', str(started)],
        env=loop_env(volume, interval=interval),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_a_sync_request_cuts_the_wait_short(volume: Volume) -> None:
    started = int(time.time()) - 5
    proc = wait_after_pass(volume, started, interval=60)
    time.sleep(1.5)
    assert proc.poll() is None, 'the wait ended before anything asked it to'

    volume.request_sync(time.time())

    # One poll step is a second here; three is plenty and nowhere near 60.
    assert proc.wait(timeout=3) == 0


def test_a_request_during_the_pass_is_answered_at_once(volume: Volume) -> None:
    """Made after the pass started, so the pass may already have fetched
    the feed someone was waiting on. The next pass must not wait."""
    started = int(time.time()) - 10
    volume.request_sync(started + 5)

    began = time.monotonic()
    assert wait_after_pass(volume, started, interval=60).wait(timeout=3) == 0
    assert time.monotonic() - began < 1


def test_a_request_in_the_second_the_pass_started_still_counts(volume: Volume) -> None:
    """The loop only has whole seconds, so this could be either side of the
    start. Pulling again is the safe guess."""
    started = int(time.time()) - 10
    volume.request_sync(started)

    assert wait_after_pass(volume, started, interval=60).wait(timeout=3) == 0


def test_a_request_the_last_pass_answered_is_not_answered_twice(volume: Volume) -> None:
    started = int(time.time())
    volume.request_sync(started - 30)

    assert wait_after_pass(volume, started, interval=1).wait(timeout=5) == 1


def test_no_request_waits_the_whole_interval(volume: Volume) -> None:
    began = time.monotonic()
    assert wait_after_pass(volume, int(time.time()), interval=1).wait(timeout=5) == 1
    assert time.monotonic() - began >= 1
