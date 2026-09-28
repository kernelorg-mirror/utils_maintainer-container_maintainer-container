#!/usr/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
# Start over: empty the data volume, so the next start is a first start.
# The dashboard asks for an email address again, and every tree and
# archive is fetched again.
#
#   systemctl --user stop maint        # or: podman stop maint
#   podman run --rm -it -v maint:/data maintainer-container powerwash
#   systemctl --user start maint       # or: podman start maint
#
# Everything goes except grokmirror's object stores, and preloads that
# are waiting to become one. That is git history, the expensive part: a
# first mirror of a kernel tree is a 4 GB download, and it is the same
# 4 GB after a powerwash as before. The repos that use the history go,
# so as far as the dashboard can tell, nothing is mirrored. When a tree
# is picked again, grok-pull finds its object store already there and
# fetches only what is new, the same way it takes over a preload. An
# object store that nothing is picked for again is removed by grok-fsck,
# the same as when a tree is deselected. --remove-git removes the history
# too.
#
# The venv goes as well: the entrypoint makes a new one on the next start,
# and a broken venv is one of the reasons to be here.
#
# It runs in the image, like `preload', rather than as a script on the
# host, so it works the same without a git checkout. The price is that it
# cannot stop the container itself, since that is the host's business. So
# it checks instead: the entrypoint holds a lock on $LOCK for as long as
# the container runs, and nothing is removed unless that lock is free.
# Pulling files out from under grok-pull or lei is how archives break,
# and "please stop it first" in the docs is not a check.
#
# It asks before it removes anything. -it lets you answer; --yes skips the
# question, for scripts.

set -euo pipefail

LOCK="$DATA_DIR/.lock"

die() {
    echo "powerwash: $*" >&2
    exit 1
}

yes=no
remove_git=no
for arg in "$@"; do
    case "$arg" in
        --yes) yes=yes ;;
        --remove-git) remove_git=yes ;;
        *) die "unknown argument '$arg' (the options are --yes and --remove-git)" ;;
    esac
done

[ -d "$DATA_DIR" ] && [ -w "$DATA_DIR" ] ||
    die "$DATA_DIR is not there -- mount the volume with -v maint:$DATA_DIR"

exec 9>>"$LOCK"
flock --nonblock 9 ||
    die "the container is still running on this volume -- stop it first (systemctl --user stop maint, or podman stop maint)"

# The lock file always stays: it is what keeps a container started right
# now from running on a half-empty volume.
keep=("$LOCK")
if [ "$remove_git" = no ]; then
    keep+=("${GROKMIRROR_OBJSTORE%/}" "${GROKMIRROR_PRELOAD_DIR%/}")
fi

# What is left once the kept paths are taken out. Directories that hold a
# kept path are gone through; everything else goes whole. A symlink is
# never followed, only removed, so nothing outside the volume is touched.
doomed=()
collect() {
    local entry k kept inside
    for entry in "$1"/*; do
        kept=no
        inside=no
        for k in "${keep[@]}"; do
            if [ "$entry" = "$k" ]; then
                kept=yes
            elif [[ "$k" == "$entry"/* ]] && [ -d "$entry" ] && [ ! -L "$entry" ]; then
                inside=yes
            fi
        done
        if [ "$kept" = yes ]; then
            continue
        elif [ "$inside" = yes ]; then
            collect "$entry"
        else
            doomed+=("$entry")
        fi
    done
}
shopt -s nullglob dotglob
collect "$DATA_DIR"
shopt -u nullglob dotglob

# The history that stays, as "3.5G", or empty if there is none. An empty
# objstore directory is no history, just a place for some.
kept=()
for k in "${keep[@]:1}"; do
    [ -d "$k" ] && kept+=("$k")
done
history=
if [ ${#kept[@]} -gt 0 ] && [ -n "$(find "${kept[@]}" -mindepth 1 -print -quit)" ]; then
    history=$(du -shc "${kept[@]}" | tail -n1 | cut -f1)
fi

if [ ${#doomed[@]} -eq 0 ]; then
    # The image declares VOLUME /data, so without -v podman mounts a new,
    # empty volume there rather than nothing. Wiping that would be
    # harmless, and "done" would be a lie about the volume the user meant.
    if [ -n "$history" ]; then
        echo "powerwash: nothing to remove but git history ($history) -- add --remove-git to remove that too" >&2
    else
        echo "powerwash: $DATA_DIR is already empty, nothing to do -- if that is a surprise, check the -v option" >&2
    fi
    exit 0
fi

if [ "$yes" = no ]; then
    [ -t 0 ] ||
        die "this asks before it removes anything -- run it with -it to answer, or add --yes"
    size=$(du -shc "${doomed[@]}" 2>/dev/null | tail -n1 | cut -f1)
    if [ -n "$history" ]; then
        printf 'This removes your settings, the mail archives and the mirrored trees (%s).\nThe git history they share is kept (%s), so trees you pick again download only what is new.\nTo remove that too, run it again with --remove-git.\n' \
            "$size" "$history" >&2
    else
        printf 'This removes everything in the volume (%s): your settings, the mail archives and the git trees.\n' \
            "$size" >&2
    fi
    printf 'Type "powerwash" to go ahead: ' >&2
    read -r answer || answer=
    [ "$answer" = powerwash ] || die "not confirmed, nothing was removed"
fi

rm -rf -- "${doomed[@]}"

if [ -n "$history" ]; then
    echo "powerwash: done, and $history of git history is kept. Start the container again, and it sets itself up from the beginning." >&2
else
    echo "powerwash: the volume is empty. Start the container again, and it sets itself up from the beginning." >&2
fi
