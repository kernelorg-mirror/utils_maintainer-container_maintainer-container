#!/usr/bin/bash
# The one externally-published web port: public-inbox-netd loads
# router.psgi instead of defaulting to bare PublicInbox::WWW, so one
# daemon fronts the dashboard, cgit, git smart/dumb-HTTP clones, and the
# mail archive (see router.psgi for how those get split by path). The
# lfit copr's public-inbox package only ships the unified
# public-inbox-netd -- no standalone public-inbox-httpd binary -- but its
# `-l http://ADDRESS/?psgi=FILE` form loads an arbitrary PSGI app the same
# way public-inbox-httpd would.
#
# The same daemon also serves whichever of IMAP and NNTP the maintainer
# has turned on in the dashboard: Daemon.pm dispatches each -l on its URL
# scheme (load_mod maps imap:// to PublicInbox::IMAP, nntp:// to
# PublicInbox::NNTP), so those are extra listeners here rather than extra
# daemons -- which is also why WEB_WORKERS is not only about the web: a
# request that blocks a worker blocks whatever mail connections that
# worker was holding too. Which ones are on is read from DAEMONS_PATH at
# startup, so a
# change there means restarting netd -- the same config-mtime watch
# grok-pull-loop.sh uses, and for the same reason.
#
# PI_CONFIG is watched too, but answered with SIGHUP rather than a
# restart: Daemon.pm binds HUP to a refresh that clears the config dedupe
# cache and calls refresh_groups on every listener, which builds a fresh
# PublicInbox::Config. So a rewritten inbox list -- a newly tracked
# subsystem, or newsgroup names appearing for the first time -- reaches
# the running daemon without dropping a connection. Without this, the
# config on disk and the config netd is serving drift apart silently, and
# a maintainer who just tracked a subsystem sees nothing change until
# something else happens to restart the daemon.
#
# This has to come up immediately, even before a public-inbox config
# exists -- / is the initial setup flow itself, so it can't wait on the
# maintainer having tracked a subsystem yet. PublicInbox::Config->new
# tolerates a missing/nonexistent PI_CONFIG path fine (it just yields an
# empty config, so /lore and /cgit 404 until write_publicinbox_config runs
# for the first time); only restart on exit, the way grok-pull-loop.sh
# restarts grok-pull.

set -uo pipefail

set -a
. /usr/local/lib/maintainer-container/defaults.env
set +a

# How often to look in on the enabled-daemons file, in seconds. Ticking
# this often costs one stat; the point is that flipping a switch in the
# dashboard visibly does something rather than waiting on a restart.
daemons_poll="${DAEMONS_POLL_INTERVAL:-5}"

# Timestamp of the enabled-daemons file, or the empty string when there
# isn't one yet. Both states have to compare equal to themselves, so that
# creating the file for the first time reads as a change like any other.
daemons_stamp() {
    stat -c %Y "$DAEMONS_PATH" 2>/dev/null || echo ''
}

# Same, for the generated public-inbox config. Missing is a normal state
# here -- it does not exist until the first subsystem is tracked.
config_stamp() {
    stat -c %Y "$PI_CONFIG" 2>/dev/null || echo ''
}

# Extra -l arguments for whatever the maintainer has turned on. Unknown
# names are ignored rather than fatal: this file is written by a newer or
# older dashboard than this script just as easily as by a matching one.
daemon_listeners() {
    [ -f "$DAEMONS_PATH" ] || return 0
    python3 -c '
import json, os, sys

ports = {"imap": os.environ["IMAP_PORT"], "nntp": os.environ["NNTP_PORT"]}
try:
    with open(sys.argv[1]) as f:
        enabled = json.load(f).get("enabled", [])
except (OSError, ValueError):
    sys.exit(0)
for name in enabled:
    port = ports.get(name)
    if port:
        print(f"-l\n{name}://0.0.0.0:{port}")
' "$DAEMONS_PATH"
}

while true; do
    stamp=$(daemons_stamp)

    # readarray over a newline-separated list, so nothing has to survive
    # word splitting -- these are URLs, but the habit is what keeps a path
    # with a space in it from quietly becoming two arguments.
    readarray -t extra < <(daemon_listeners)

    public-inbox-netd -W"$WEB_WORKERS" \
        -l "http://0.0.0.0:$ROUTER_PORT/?env.PI_CONFIG=$PI_CONFIG,psgi=$ROUTER_SCRIPT" \
        "${extra[@]}" &
    pid=$!

    cfg_stamp=$(config_stamp)

    restarting=''
    while kill -0 "$pid" 2>/dev/null; do
        sleep "$daemons_poll"

        # The inbox list is a reload, not a restart, so handle it first
        # and stay in the loop.
        now=$(config_stamp)
        if [ "$now" != "$cfg_stamp" ]; then
            cfg_stamp="$now"
            echo "serve-web: public-inbox config changed, reloading" >&2
            kill -HUP "$pid"
        fi

        [ "$(daemons_stamp)" = "$stamp" ] && continue

        echo "serve-web: enabled daemons changed, restarting public-inbox-netd" >&2
        kill "$pid"
        restarting='yes'
        break
    done

    wait "$pid" 2>/dev/null

    if [ -z "$restarting" ]; then
        echo "serve-web: public-inbox-netd exited, restarting in 30s" >&2
        sleep 30
    fi
done
