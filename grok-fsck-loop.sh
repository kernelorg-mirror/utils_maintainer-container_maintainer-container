#!/usr/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
# grok-fsck has no continuous mode of its own -- production runs it off a
# weekly systemd timer and lets [fsck] frequency (days) spread the actual
# connectivity/repack work across repos so each invocation is cheap. No
# timers here, so just call it once a day and let the same frequency
# setting do the spreading.

set -uo pipefail

set -a
. /usr/local/lib/maintainer-container/defaults.env
set +a

while sleep "$GROKMIRROR_FSCK_INTERVAL"; do
    if [ -f "$GROKMIRROR_CONF_PATH" ]; then
        grok-fsck -c "$GROKMIRROR_CONF_PATH" || echo "grok-fsck-loop: run failed, will retry next interval" >&2
    fi
done
