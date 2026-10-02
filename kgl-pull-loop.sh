#!/usr/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
# Keeps the mail feeds current. That is `kgl pull' rather than lei's own
# `lei up --all' -- korgalore already wraps the feed refresh and delivers to
# whatever target is configured. Only takes effect once the dashboard has
# tracked at least one subsystem and korgalore.toml has a target in it;
# before that, kgl pull would just abort.
#
# public-inbox-extindex runs right after, chained rather than on its own
# timer -- there's no point re-indexing until lei has actually pulled
# new mail into the v2 archives extindex reads from. Skipped the same way,
# gated on the dashboard having written a public-inbox config yet.
#
# Most pulls bring nothing new, so extindex is gated further on the archives
# having actually changed since the last successful index. The signal is a
# stamp file rather than kgl's exit status: a pull can fail after some of its
# deliveries already landed mail, and mail can also arrive by other means
# (the dashboard tracking a new subsystem), so what matters is whether
# anything under the lei archives is newer than the last index -- not how the
# pull that may or may not have produced it ended.
#
# The stamp is dated before indexing starts, not after, so mail that lands
# while extindex is running still looks new on the next pass instead of being
# skipped until something else touches the archives.
#
# The interval is slept at the end of a pass, not the start of one. Sleeping
# first meant a restarted container spent the first ten minutes with whatever
# state it came up with, and an extindex that is missing or behind is not a
# cosmetic problem: PublicInbox::WWW's front page opens it to build the
# listing, so /lore/ answers 500 until something builds it. Both guards below
# are cheap and both no-op when there is nothing to do, so an immediate first
# pass costs a restart nothing.
#
# A pass that went fully right also moves the pull stamp (PULL_STAMP_PATH),
# which router.psgi sends as `updated=' in X-Archive-Coverage. It tells a
# client that every thread the mirror has is current up to that time, so the
# bar is high, and anything short of it leaves the stamp where it was --
# which is always safe, the client just asks upstream more:
#
#   - `kgl pull --fail-on-feed-error' exited 0. Without the flag, kgl pull
#     exits 0 even when a feed failed to update. Any other status counts as
#     a failure; there is no telling a real one from an unknown flag, and
#     there is no need to.
#   - The extindex is current: it ran and worked, or nothing had changed.
#     /lore/all/ only shows what is in it.
#   - Every archive /lore serves is one the pull updates (lore_coverage.py),
#     checked both before and after the pull, so a subsystem paused or
#     resumed in the middle of it can't slip through either way.
#
# The stamp is the time the pass started, not ended: mail that reached
# upstream during the pull may or may not be in it.
#
# The dashboard's "Sync now" button asks for a pass without running one:
# it touches SYNC_REQUEST_PATH, and the wait between passes looks at that
# every SYNC_POLL_INTERVAL seconds and cuts itself short when it is newer
# than the start of the last pass. The dashboard can't just run `kgl pull'
# itself. It would race the pull this loop may already have going, and
# nothing would move the stamp or run extindex after it, so the new mail
# would sit in the archives without showing up in /lore/all/.
#
# A request made while a pass is running counts as newer than that pass,
# so a second pass follows straight after it. That is on purpose: the pass
# under way may have already fetched the feed somebody was waiting on, and
# "Sync now" has to mean "everything up to when I pressed it". A request in
# the same second a pass started also counts, for the same reason. At
# worst that is one pass more than needed, and passes with nothing new are
# cheap.
#
# Each pass also writes KGL_PULL_STATE_PATH: just `started=' when it
# begins, and `started=', `finished=' and `ok=' when it ends. That is what
# the dashboard reads to tell "still waiting", "pulling" and "done" apart.
# The file is replaced whole, so it never holds half of one and half of the
# other.
#
# `--once' runs a single pass and exits with 0 only if it moved the stamp,
# for the tests. `--wait STARTED' runs just the wait after a pass that
# started at STARTED (epoch seconds), and exits with 0 if a sync request cut
# it short, also for the tests. DEFAULTS_ENV is for them too: outside the
# container defaults.env is not where the line below expects it.

set -uo pipefail

set -a
. "${DEFAULTS_ENV:-/usr/local/lib/maintainer-container/defaults.env}"
set +a

# Only files count, and only files extindex would actually read: every pull
# rewrites korgalore's own korgalore.feed/korgalore.lock inside each archive
# (which also bumps the directory's mtime) whether or not any mail arrived,
# so watching those would mean never skipping a single run. A brand new
# archive still registers -- it arrives with a whole v2 layout of its own.
archives_changed() {
    [ -d "$LEI_ARCHIVES_PATH" ] || return 1
    # Never indexed yet -- anything there is new by definition.
    [ -f "$EXTINDEX_STAMP_PATH" ] || return 0
    [ -n "$(find "$LEI_ARCHIVES_PATH" -type f ! -name 'korgalore.*' \
        -newer "$EXTINDEX_STAMP_PATH" -print -quit 2>/dev/null)" ]
}

covered() {
    "$VENV_DIR/bin/python" "$LORE_COVERAGE_SCRIPT" "$PI_CONFIG" "$KORGALORE_CONF_PATH"
}

# Written whole and renamed into place, so the dashboard never reads half
# a state.
record_state() {
    printf '%s\n' "$@" > "$KGL_PULL_STATE_PATH.new" &&
        mv -f "$KGL_PULL_STATE_PATH.new" "$KGL_PULL_STATE_PATH"
}

# One pass. Succeeds only when it is good enough to move the stamp.
pull_pass() {
    local ok=1
    pass_started=$(date +%s)
    record_state "started=$pass_started"

    if [ ! -f "$KORGALORE_CONF_PATH" ]; then
        # Nothing tracked yet, so nothing was pulled.
        ok=0
    else
        covered || ok=0
        if ! "$KGL" pull --fail-on-feed-error; then
            echo "kgl-pull-loop: pull failed, will retry next interval" >&2
            ok=0
        fi
    fi
    if [ -f "$PI_CONFIG" ] && archives_changed; then
        touch "$EXTINDEX_STAMP_PATH.new"
        if public-inbox-extindex "$PUBLICINBOX_EXTINDEX_PATH" --all; then
            mv "$EXTINDEX_STAMP_PATH.new" "$EXTINDEX_STAMP_PATH"
        else
            rm -f "$EXTINDEX_STAMP_PATH.new"
            echo "kgl-pull-loop: extindex failed, will retry next interval" >&2
            ok=0
        fi
    fi
    if [ "$ok" = 1 ] && covered; then
        # Written whole and renamed into place, so router.psgi never reads
        # half a number -- and the rename gives it a new inode to notice.
        printf '%s\n' "$pass_started" > "$PULL_STAMP_PATH.new" &&
            mv -f "$PULL_STAMP_PATH.new" "$PULL_STAMP_PATH" || ok=0
    else
        ok=0
    fi

    record_state "started=$pass_started" "finished=$(date +%s)" "ok=$ok"
    [ "$ok" = 1 ]
}

# Whether the dashboard asked for a sync since the last pass started.
sync_requested() {
    local requested
    requested=$(stat -c %Y "$SYNC_REQUEST_PATH" 2>/dev/null) || return 1
    [ "$requested" -ge "$pass_started" ]
}

# Wait out KGL_PULL_INTERVAL, a step at a time, so a sync request is seen
# within one step. Returns 0 if a request cut the wait short. It looks
# before the first step, so a request made during the pass is answered
# straight away.
wait_for_next_pass() {
    local waited=0
    while [ "$waited" -lt "$KGL_PULL_INTERVAL" ]; do
        if sync_requested; then
            echo "kgl-pull-loop: sync requested, pulling now" >&2
            return 0
        fi
        sleep "$SYNC_POLL_INTERVAL"
        waited=$((waited + SYNC_POLL_INTERVAL))
    done
    return 1
}

case "${1:-}" in
--once)
    pull_pass
    exit
    ;;
--wait)
    pass_started=$2
    wait_for_next_pass
    exit
    ;;
esac

while :; do
    pull_pass
    wait_for_next_pass
done
