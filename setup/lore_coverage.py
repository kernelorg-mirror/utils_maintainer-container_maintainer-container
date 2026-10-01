#!/data/venv/bin/python
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
"""Is every archive /lore serves one that `kgl pull' keeps current?

kgl-pull-loop.sh asks this before it moves the pull stamp, the `updated='
in X-Archive-Coverage. That stamp promises a client that a thread the
mirror has got every new reply up to that time, so it may skip asking
upstream. The promise only holds for archives the pull actually refreshed.
The public-inbox config and korgalore's config are written at different
times, so they can disagree: a subsystem paused from the command line
(its conf.d toml renamed to *.toml.paused) is still served, but no longer
pulled, and its threads would go stale behind a fresh stamp.

An archive counts as pulled when a delivery in the config `kgl pull'
loads points at it through a `lei:' feed -- korgalore only opens the
feeds some delivery uses. The config is read with korgalore's own
load_config and resolve_feed_url, not by hand, so this sees exactly the
files and the merge order `kgl pull' sees, and stays right if those change.

Exit status: 0 when every served archive is pulled, 1 when one is not
(each one is named on stderr), 2 when either config can't be read. Only 0
lets the stamp move. Nothing is cached: the files are small, and a change
to them has to count on the very next cycle.
"""

import logging
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set

from korgalore.cli import load_config, resolve_feed_url

logger = logging.getLogger('lore-coverage')

GIT_TIMEOUT = 30


class ConfigError(Exception):
    pass


def served_archives(pi_config: Path) -> Dict[str, Path]:
    """Every inbox in the public-inbox config, by name, with its inboxdir.

    Read with git-config, the way PublicInbox::Config reads it, so an
    inbox counts here exactly when /lore can see it. No config yet means
    nothing is served.
    """
    if not pi_config.exists():
        return {}
    try:
        proc = subprocess.run(
            ['git', 'config', '-f', str(pi_config), '-z', '--get-regexp', r'^publicinbox\..*\.inboxdir$'],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise ConfigError(f'could not read {pi_config}: {e}') from e
    # 1 is "nothing matched": a config with no inboxes in it yet.
    if proc.returncode == 1:
        return {}
    if proc.returncode != 0:
        raise ConfigError(f'git config failed on {pi_config}: {proc.stderr.strip()}')

    served: Dict[str, Path] = {}
    for record in proc.stdout.split('\0'):
        if not record:
            continue
        key, _, value = record.partition('\n')
        # publicinbox.<name>.inboxdir, and <name> may have dots in it.
        name = key[len('publicinbox.') : -len('.inboxdir')]
        served[name] = Path(value).resolve()
    return served


def pulled_archives(korgalore_conf: Path) -> Set[Path]:
    """The archive of every `lei:' feed that some delivery uses."""
    if not korgalore_conf.exists():
        # load_config logs this one but doesn't stop on it.
        raise ConfigError(f'{korgalore_conf} does not exist')
    try:
        config = load_config(korgalore_conf)
        pulled: Set[Path] = set()
        for details in config.get('deliveries', {}).values():
            url = resolve_feed_url(details.get('feed', ''), config)
            if url.startswith('lei:'):
                pulled.add(Path(url[len('lei:') :]).resolve())
    except Exception as e:
        # A config `kgl pull' would choke on too (click.Abort from
        # load_config, ConfigurationError from resolve_feed_url).
        # korgalore has logged why.
        raise ConfigError(f'could not load {korgalore_conf}: {e!r}') from e
    return pulled


def uncovered(pi_config: Path, korgalore_conf: Path) -> List[str]:
    """The served archives `kgl pull' doesn't refresh, by name."""
    served = served_archives(pi_config)
    if not served:
        return []
    pulled = pulled_archives(korgalore_conf)
    return sorted(name for name, path in served.items() if path not in pulled)


def main(argv: List[str]) -> int:
    logging.basicConfig(format='lore-coverage: %(message)s', level=logging.WARNING)
    if len(argv) != 2:
        print('usage: lore_coverage.py <public-inbox config> <korgalore.toml>', file=sys.stderr)
        return 2
    try:
        missing = uncovered(Path(argv[0]), Path(argv[1]))
    except ConfigError as e:
        logger.warning('%s', e)
        return 2
    for name in missing:
        logger.warning('%s is served under /lore but kgl pull does not update it', name)
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
