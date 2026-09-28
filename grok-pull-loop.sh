#!/usr/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
# The one owner of the grok-pull process.
#
# Nothing else may start grok-pull against this toplevel. It is tempting
# for the dashboard to write grokmirror.conf and then run a one-shot
# grok-pull of its own, to get the new repos going sooner. But this loop
# has a continuous one going, and two grok-pulls on one toplevel is a race
# neither is built to survive: a one-shot run that finds a repo locked by
# the other silently drops it --
#
#     except grokmirror.GrokLockError:
#         if not runonce:
#             held.append((gitdir, repoinfo, q_action))
#         return
#
# -- with no error and nothing left in its todo list, so it exits 0 having
# mirrored one repo of three and reports a complete mirror. So the
# dashboard only writes the config; picking that up is this loop's job.
#
# grok-pull reads its config once at startup -- cull_manifest() applies
# [pull] include from the object command() parsed -- so a changed selection
# reaches a running grok-pull only through a restart. Watching the config's
# mtime is what turns "the maintainer picked different repos" into one.

set -uo pipefail

set -a
. /usr/local/lib/maintainer-container/defaults.env
set +a

# How often to look in on the config while grok-pull runs, in seconds.
# Short enough that picking repos in the dashboard visibly does something,
# small enough next to a clone to cost nothing.
conf_poll="${GROKMIRROR_CONF_POLL_INTERVAL:-5}"

while true; do
    if [ ! -f "$GROKMIRROR_CONF_PATH" ]; then
        # No repos picked yet -- the dashboard writes this on first use.
        sleep "$conf_poll"
        continue
    fi

    stamp=$(stat -c %Y "$GROKMIRROR_CONF_PATH")

    # Say what is wrong with the config before running on it. The
    # dashboard already checks whatever it writes, but nothing makes this
    # file come from the dashboard -- it sits on the volume and a
    # maintainer can edit it -- and a hand-written config is exactly what
    # --config-check exists for. It reports every problem at once rather
    # than dying on the first, writes nothing and runs nothing, so it is
    # safe to do here on every restart.
    #
    # Advisory: a bad report never stops the pull. grok-pull is about to
    # render its own verdict, and refusing to start on a config grokmirror
    # itself would have accepted would make this loop the stricter of the
    # two. The exit code is read only to tell a verdict apart from a check
    # that never ran. Offline, because a mirror that cannot reach the
    # remote site has a problem grok-pull reports far better than a
    # pre-flight check can, and because this runs on every restart.
    #
    # Quiet when there is nothing to say: this loop restarts on every
    # reselection and every 30s after a failed pull, so a clean report
    # printed in full each time would be three lines of noise on exactly
    # the systems whose logs are already worth reading.
    check_report=$(grok-pull -c "$GROKMIRROR_CONF_PATH" \
                       --config-check --no-network 2>&1)
    check_rc=$?

    # run_check exits 0 with no errors and 1 with some; anything else did
    # not get as far as checking. A grokmirror predating --config-check
    # exits 2 with argparse usage, which contains none of the words below
    # and would otherwise be reported as a clean config.
    if [ "$check_rc" -gt 1 ]; then
        echo "grok-pull-loop: could not check the config" \
             "(grok-pull --config-check exited $check_rc);" \
             "continuing anyway" >&2
    elif grep -qE '^ +(error|warning):' <<<"$check_report"; then
        sed 's/^/grok-pull-loop: /' <<<"$check_report" >&2
    else
        echo "grok-pull-loop: config checks out" >&2
    fi

    grok-pull -o -c "$GROKMIRROR_CONF_PATH" &
    pid=$!

    # Written for the dashboard, which reports whether a mirror is under
    # way rather than running one itself. It checks /proc as well, since an
    # unclean exit leaves this file behind.
    mkdir -p "$GROKMIRROR_DIR"
    echo "$pid" > "$GROKMIRROR_PID_PATH"

    restarting=''
    while kill -0 "$pid" 2>/dev/null; do
        sleep "$conf_poll"
        now=$(stat -c %Y "$GROKMIRROR_CONF_PATH" 2>/dev/null) || continue
        [ "$now" = "$stamp" ] && continue

        echo "grok-pull-loop: repo selection changed, restarting grok-pull" >&2
        # SIGTERM rather than SIGKILL: grok-pull's own handler writes out
        # the manifest for everything it has already fetched before it
        # exits, so a reselection doesn't discard a finished clone.
        kill "$pid"
        restarting='yes'
        break
    done

    wait "$pid" 2>/dev/null
    rm -f "$GROKMIRROR_PID_PATH"

    if [ -z "$restarting" ]; then
        # Exited on its own. In continuous mode that means it failed, so
        # back off instead of spinning on whatever went wrong.
        echo "grok-pull-loop: grok-pull -o exited, restarting in 30s" >&2
        sleep 30
    fi
done
