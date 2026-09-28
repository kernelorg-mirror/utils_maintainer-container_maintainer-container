#!/usr/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
# Build a grokmirror object store out of a clone the maintainer already has,
# so the first mirror of a big tree does not have to download all of it.
#
#   podman run --rm --security-opt label=disable \
#       -v maint:/data -v ~/linux:/seed:ro \
#       maintainer-container preload [UPSTREAM-PATH]
#
# The clone can be any recent Linux clone: mainline, a subsystem tree, a
# personal fork. What makes a mirror's first pull big is old history, and
# every one of them has that. UPSTREAM-PATH is the repo on $GROKMIRROR_SITE
# whose history is the one worth having, and there is rarely a reason to
# change it from mainline. The result lands in $GROKMIRROR_PRELOAD_DIR under that same
# path, and the dashboard moves it into place as the object store once any
# repo that shares one with it is picked -- for mainline, that is nearly
# every kernel tree (see adopt_preloads() in setup/app.py). grok-pull then
# finds an object store already there and fetches only what the clone was
# missing.
#
# What gets copied starts from upstream, not from the clone: every commit
# upstream advertises (its branch heads, and the commit behind each tag)
# that the clone also has, and the history behind those. Nothing the
# maintainer has only locally can be reached from there, so their own
# branches stay off the volume, and a clone of some unrelated tree gives an
# empty result rather than a pile of objects nobody wants. The clone's own
# refs are never used as starting points, and nor are their names: a clone
# fetched without tags, or with branches named anything at all, works the
# same.
#
# Only commits the clone's refs can reach, though. Git promises complete
# history for what is reachable and nothing else -- `gc' may prune the
# parent of an unreachable commit and keep the commit -- and pack-objects
# would fail halfway through on the first hole.
#
# Plumbing only, the same way grokmirror's objstore_uses_plumbing works.
# grokmirror itself copies by hardlinking packs, which cannot work here:
# the clone is on another filesystem, and its packs hold everything, the
# private bits included. So the clone is borrowed as an alternate instead,
# and pack-objects writes one pack of exactly what those commits reach,
# reusing the clone's deltas as it goes. That is what `clone --dissociate'
# does under the hood (via repack), without the fetch -- a fetch would
# index-pack all of it again, which is most of the time a network clone
# takes. No bitmap either: that is the other slow part, and grok-fsck
# writes one on its first full repack anyway.
#
# --security-opt label=disable is for SELinux hosts: without it the clone
# is unreadable in the container, and `:z' would fix that by relabelling the
# maintainer's own clone, which is not ours to change.

set -euo pipefail

# Settable so the tests can point it somewhere else.
SEED=${PRELOAD_SEED:-/seed}
upstream=${1:-/pub/scm/linux/kernel/git/torvalds/linux.git}

die() {
    echo "preload: $*" >&2
    exit 1
}

case "$upstream" in
    /*.git) ;;
    *) die "expected a repo path like /pub/scm/linux/kernel/git/torvalds/linux.git, not '$upstream'" ;;
esac
case "/$upstream/" in
    */../* | */./*) die "'$upstream' is not a plain path" ;;
esac

# The common dir, not the git dir: a worktree's own git dir has no objects.
seed_git=$(git -C "$SEED" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) ||
    die "no git repository at $SEED -- mount your clone with -v PATH:$SEED:ro"
seed_objects="$seed_git/objects"

# A clone made with --reference (or by `git worktree' off one that was)
# keeps some of its objects in another repo, named by a host path that is
# not mounted here. Nothing would notice until pack-objects came across an
# object it cannot find, halfway through.
if [ -f "$seed_objects/info/alternates" ]; then
    while read -r alt; do
        case "$alt" in '' | '#'*) continue ;; esac
        [ -d "$alt" ] ||
            die "$SEED borrows objects from $alt, which is not mounted -- mount it at the same path too, or use a clone without alternates"
    done <"$seed_objects/info/alternates"
fi

# Both leave out objects on purpose -- history for a shallow clone, blobs for
# a partial one -- and pack-objects would stop at the first. The work repo
# does not carry the clone's shallow file or promisor remote, so git has no
# way to tell it is looking at a hole rather than a corrupt repo.
[ "$(git -C "$SEED" rev-parse --is-shallow-repository)" = false ] ||
    die "$SEED is a shallow clone -- it has no old history to copy; use a full clone"
# Older git names the promisor remote in extensions.partialClone, newer git
# marks it with remote.<name>.promisor, and the packs fetched from it come
# with a .promisor file either way.
if git -C "$SEED" config --get extensions.partialClone >/dev/null ||
    git -C "$SEED" config --type=bool --get-regexp '^remote\..*\.promisor$' 2>/dev/null | grep -q ' true$' ||
    compgen -G "$seed_objects/pack/*.promisor" >/dev/null; then
    die "$SEED is a partial clone and is missing objects on purpose -- use a full clone"
fi

dest="$GROKMIRROR_PRELOAD_DIR$upstream"
if [ -e "$dest" ]; then
    echo "preload: $dest is already there, nothing to do" >&2
    exit 0
fi
if [ -e "$GROKMIRROR_OBJSTORE" ] && [ -n "$(ls -A "$GROKMIRROR_OBJSTORE")" ]; then
    echo "preload: note: this volume already mirrors something; the preload is only used if $upstream gets an object store of its own" >&2
fi

# Built under a name the dashboard does not look for, and renamed into
# place only once it is complete, so a preload that was interrupted can
# never be adopted.
parent=$(dirname "$dest")
name=$(basename "$dest" .git)
work="$parent/.incomplete-$name.git"
# grokmirror's locked_repo() takes its lock on a file beside the repo and
# leaves the file behind.
lock="$parent/..incomplete-$name.git.lock"
mkdir -p "$parent"
rm -rf "$work" "$lock"
trap 'rm -rf "$work" "$lock"' EXIT

echo "preload: asking $GROKMIRROR_SITE what $upstream has" >&2
# The one bit of network this needs: upstream's refs, a few hundred KB.
# Without --refs, so each annotated tag's commit comes along as a ^{} line.
upstream_refs=$(git ls-remote "$GROKMIRROR_SITE$upstream") ||
    die "could not list the refs of $GROKMIRROR_SITE$upstream -- is the path right, and is the site reachable?"

# grokmirror's own setup, so the object store is configured exactly the way
# one grok-pull created would be. setup_objstore_repo() adds the .git.
python3 -c 'import sys, grokmirror; grokmirror.setup_objstore_repo(sys.argv[1], sys.argv[2])' \
    "$parent" ".incomplete-$name" ||
    die "could not create an object store in $work"

echo "$seed_objects" >"$work/objects/info/alternates"

# Upstream's commits that the clone has. cat-file says "missing" for the
# rest. The tag objects themselves are left for grok-pull, since each one
# is a few hundred bytes and the commit it points at is listed anyway.
present=$(printf '%s\n' "$upstream_refs" | cut -f1 | LC_ALL=C sort -u |
    git -C "$SEED" cat-file --batch-check='%(objectname) %(objecttype)' |
    awk '$2 == "commit" { print $1 }')

# And of those, the ones the clone's refs reach. rev-list prints what it can
# get to from the candidates but not from any ref, so every candidate that
# turns up is one to drop. --not applies to --all only: revisions read from
# --stdin are never affected by it.
reachable=$(LC_ALL=C comm -23 \
    <(printf '%s\n' "$present" | sed '/^$/d') \
    <(printf '%s\n' "$present" | sed '/^$/d' |
        git -C "$SEED" rev-list --stdin --not --all | LC_ALL=C sort))

# Under refs/virtual/, where grokmirror keeps every repo's refs, but in a
# namespace no real repo can have (virtrefs are hex): the first grok-fsck
# after the mirror is up drops these as stale, and its full repack keeps
# whatever the real mirror still reaches -- all of it, in practice. Named
# after the object, so nothing read off the network becomes a ref name.
printf '%s\n' "$reachable" | sed '/^$/d' |
    awk '{ print "create refs/virtual/preload/" $1 " " $1 }' |
    git -C "$work" update-ref --stdin

count=$(git -C "$work" for-each-ref --format=x refs/virtual/preload/ | wc -l)
[ "$count" -gt 0 ] ||
    die "$SEED has none of the history of $upstream -- is it a clone of that tree?"

echo "preload: copying the history behind $count of upstream's commits (this takes a few minutes)" >&2
git -C "$work" for-each-ref --format='%(objectname)' refs/virtual/preload/ |
    git -C "$work" pack-objects --revs --delta-base-offset --quiet objects/pack/pack >/dev/null

rm "$work/objects/info/alternates"
mv "$work" "$dest"
rm -f "$lock"
trap - EXIT

echo "preload: done -- $(du -sh "$dest" | cut -f1) in $dest" >&2
echo "preload: it is used for the first tree you pick that shares objects with $upstream" >&2
