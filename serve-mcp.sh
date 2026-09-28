#!/usr/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
# The MCP server, on its own internal port with router.psgi in front of it
# (see mcpd/server.py for why the transport is HTTP and not stdio).
#
# A restart loop rather than a one-shot, for the same reason serve-web.sh
# is one: this comes up before there is anything to serve. On a first boot
# there is no public-inbox config and no extindex yet, and the server's
# answer to a query is then "this archive has nothing" -- which is the
# truth, and a better answer than a connection refused. Nothing here waits
# on the maintainer having tracked a subsystem.
#
# It runs out of /data/venv rather than the system Python because that is
# where liblore and the MCP SDK are, resynced from git on every start by
# sync-venv.sh. venv-sync-loop.sh can replace both under a running
# process, so an upgrade takes effect at the next restart rather than
# immediately -- acceptable for a server whose every request is one
# question and one answer, and the alternative is restarting it daily for
# no reason.

set -uo pipefail

set -a
. /usr/local/lib/maintainer-container/defaults.env
set +a

while true; do
    "$VENV_DIR/bin/python" /usr/local/lib/maintainer-setup/mcpd/server.py
    echo "serve-mcp: the MCP server exited, restarting in 30s" >&2
    sleep 30
done
