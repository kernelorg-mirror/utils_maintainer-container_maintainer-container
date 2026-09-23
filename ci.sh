#!/usr/bin/env sh

set -eu

# Run each gate in order, stopping at the first failure.  Every step has
# its own exit code so a failure is discernible both from the printed
# banner and from ci.sh's own exit status (e.g. exit 13 == ty check).
#
# Same gates, same exit codes and same order as korgalore's ci.sh: this
# repo's Python imports korgalore's, and two codebases that read the same
# should be held to the same standard.

run() {
    # run <exit-code-on-failure> <description> <command...>
    _code="$1"
    _step="$2"
    shift 2
    printf '\n=== %s ===\n' "$_step"
    if ! "$@"; then
        printf '\n>>> CI FAILED at: %s (ci.sh exit %d)\n' "$_step" "$_code" >&2
        exit "$_code"
    fi
}

# liblore and korgalore are installed from git, matching what sync-venv.sh
# puts in the container's venv. The type checkers need korgalore in
# particular: setup/app.py imports it, and without it every one of those
# imports reads as a missing stub.
run 10 'uv sync'      uv sync --all-groups
run 11 'ruff format'  uv run ruff format --check
run 12 'ruff check'   uv run ruff check
run 13 'ty check'     uv run ty check
run 14 'mypy'         uv run mypy .
run 15 'pyright'      uv run pyright
# tests/test_router.py starts a real public-inbox-netd and skips, naming
# what is missing, on a host without it. A skip here is a test nobody is
# running -- see the README on installing the two packages it wants.
run 16 'pytest'       uv run pytest --durations=20

printf '\n=== CI PASSED ===\n'
