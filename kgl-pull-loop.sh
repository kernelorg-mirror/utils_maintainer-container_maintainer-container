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

set -uo pipefail

set -a
. /usr/local/lib/maintainer-container/defaults.env
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

while :; do
    if [ -f "$KORGALORE_CONF_PATH" ]; then
        "$KGL" pull || echo "kgl-pull-loop: pull failed, will retry next interval" >&2
    fi
    if [ -f "$PI_CONFIG" ] && archives_changed; then
        touch "$EXTINDEX_STAMP_PATH.new"
        if public-inbox-extindex "$PUBLICINBOX_EXTINDEX_PATH" --all; then
            mv "$EXTINDEX_STAMP_PATH.new" "$EXTINDEX_STAMP_PATH"
        else
            rm -f "$EXTINDEX_STAMP_PATH.new"
            echo "kgl-pull-loop: extindex failed, will retry next interval" >&2
        fi
    fi
    sleep "$KGL_PULL_INTERVAL"
done
