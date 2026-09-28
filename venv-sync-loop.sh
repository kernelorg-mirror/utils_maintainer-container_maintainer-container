#!/usr/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
# Daily "uv sync --upgrade" timer, see sync-venv.sh. Runs in the
# background for the life of the container; a failed sync logs and leaves
# the existing venv running untouched rather than tearing anything down.

set -a
. /usr/local/lib/maintainer-container/defaults.env
set +a

while sleep "$VENV_SYNC_INTERVAL"; do
    if ! /usr/local/bin/sync-venv.sh; then
        echo "venv-sync-loop: sync failed, keeping existing venv" >&2
    fi
done
