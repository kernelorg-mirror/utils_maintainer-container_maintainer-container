#!/usr/bin/bash
# Sync /data/venv on every start, start the daily resync loop, then run
# the container's real command as a child and supervise it. This is what
# makes
# --pull=always/podman auto-update safe: the image can be rebuilt or
# recreated freely, since korgalore/liblore live in the persistent /data
# volume and get resynced here rather than baked in.

set -euo pipefail

# One copy of every path and port, exported for everything below -- the
# loops, the Perl router under public-inbox-netd, and the Python dashboard
# this eventually execs into. See defaults.env for why.
set -a
. /usr/local/lib/maintainer-container/defaults.env
set +a

# A one-shot job rather than the container proper: build an object store
# out of a clone mounted at /seed, then exit. Before the venv sync, since it
# needs nothing from the venv, and before anything is started that might
# see the volume half-done. See preload-objstore.sh.
if [ "${1:-}" = preload ]; then
    shift
    exec /usr/local/bin/preload-objstore.sh "$@"
fi

# The other one-shot job: empty the volume. It refuses to while the lock
# below is held, so it cannot run under a live container. See powerwash.sh.
if [ "${1:-}" = powerwash ]; then
    shift
    exec /usr/local/bin/powerwash.sh "$@"
fi

# Held for as long as the container runs: the fd is inherited by every
# process started below, and the kernel drops the lock when the last of
# them exits. powerwash.sh takes the same lock before it removes anything.
# If it is taken, that is a powerwash in progress, or another container
# on the same volume -- either way, running now would break things, so
# wait rather than start on a volume that is being emptied.
exec 9>>"$DATA_DIR/.lock"
#
# The wait has to answer `podman stop', and nothing further down is set up
# for that yet. It also cannot be a plain `flock 9': bash runs a trap only
# once the foreground command returns, and this one may never return. A
# `wait' is interrupted by a trapped signal, so flock runs in the
# background instead. The lock still ends up ours: flock locks the open
# file, which the child shares with this shell through fd 9, so the lock
# outlives the child.
if ! flock --nonblock 9; then
    echo "entrypoint: $DATA_DIR is in use by a powerwash or another container, waiting for it" >&2
    flock 9 &
    waiter=$!
    trap 'kill "$waiter" 2>/dev/null; exit 0' TERM INT
    wait "$waiter"
    trap - TERM INT
fi

# A sync failure is only fatal when there's nothing to fall back on. The
# venv lives on the volume, so on every start but the first there's
# already a working korgalore in it -- and taking the whole container down
# because git.kernel.org was briefly unreachable (or the host is offline
# entirely) would mean losing the dashboard, cgit and the archive along
# with it, none of which need the network to serve what's already mirrored.
# This matches what venv-sync-loop.sh does with a failed daily resync.
# Test for kgl rather than bin/python: `uv venv' creates the interpreter
# without needing the network, so a first boot that got that far and then
# failed to install anything still has a bin/python -- but nothing that
# can actually serve. kgl only appears once korgalore is really installed.
if ! /usr/local/bin/sync-venv.sh; then
    if [ -x /data/venv/bin/kgl ]; then
        echo "entrypoint: venv sync failed, continuing with the existing venv" >&2
    else
        echo "entrypoint: venv sync failed and no usable venv exists yet" >&2
        exit 1
    fi
fi

# Shut down when asked, instead of being killed.
#
# This has to be a trap in a shell that stays PID 1, and the reason is a
# kernel rule that is easy to walk straight past: PID 1 does not get the
# default signal dispositions every other process gets. A signal is
# delivered to PID 1 only if PID 1 has installed a handler for it, and is
# dropped otherwise. Python installs one for SIGINT and none for SIGTERM,
# so if this script ended in `exec "$@"' -- making the dashboard PID 1 --
# every `podman stop' would be discarded in silence, and the container would
# die on the SIGKILL that follows ten seconds later.
#
# Nothing would get to finish. grok-pull's own SIGTERM handler writes out the
# manifest for the clones it has already completed, so a SIGKILL there
# means refetching them; public-inbox-netd and the lei backends would
# rather close their Xapian handles than be shot holding them open.
#
# So the shell stays PID 1, `trap' installs the handler that makes the
# kernel deliver, and "$@" runs as a child like everything else.

# How long to wait for everything to be gone, in tenths of a second. Well
# inside podman's default ten-second grace: overrunning it would land us
# back on the SIGKILL this exists to avoid.
SHUTDOWN_GRACE=50

shutdown() {
    # A second stop, or a Ctrl-C on top of one, shouldn't re-enter this.
    trap '' TERM INT

    echo "entrypoint: shutting down" >&2

    # -1 is every process in this PID namespace except PID 1 itself, which
    # is the point: the loops are our children, but what they supervise --
    # grok-pull, public-inbox-netd -- are grandchildren, and signalling
    # only our own would leave exactly the processes that need a clean exit
    # to be killed instead.
    kill -TERM -1 2>/dev/null || true

    # Our own children can be waited on. The grandchildren cannot -- an
    # orphan reparents to this shell but never joins its jobs table -- so
    # they get watched out of /proc instead. Bounded either way: a process
    # that will not leave is not a reason to hang here until podman's
    # SIGKILL arrives, since that is the outcome being avoided.
    wait 2>/dev/null || true
    for _ in $(seq "$SHUTDOWN_GRACE"); do
        alone=yes
        for proc in /proc/[0-9]*; do
            [ "${proc#/proc/}" = 1 ] || { alone=no; break; }
        done
        [ "$alone" = yes ] && break
        sleep 0.1
    done

    exit 0
}

trap shutdown TERM INT

/usr/local/bin/venv-sync-loop.sh &
/usr/local/bin/grok-pull-loop.sh &
/usr/local/bin/grok-fsck-loop.sh &
/usr/local/bin/kgl-pull-loop.sh &
/usr/local/bin/serve-web.sh &
/usr/local/bin/serve-mcp.sh &

# Not `exec': see above. Staying here as PID 1 is the whole mechanism.
"$@" &
main=$!

status=0
wait "$main" || status=$?

# Fell out on its own rather than being stopped. Take the container down
# with it -- the loops have nothing left to serve, and a container whose
# command has exited should not look healthy.
trap '' TERM INT
echo "entrypoint: the container command exited ($status), stopping" >&2
kill -TERM -1 2>/dev/null || true
exit "$status"
