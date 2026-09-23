#!/usr/bin/bash
# Create (if missing) and sync /data/venv against the live korgalore and
# liblore git repos. Idempotent -- safe to call on every container start
# and from the daily resync in venv-sync-loop.sh.

set -euo pipefail

set -a
. /usr/local/lib/maintainer-container/defaults.env
set +a

if [ ! -x "$VENV_DIR/bin/python" ]; then
    uv venv "$VENV_DIR"
fi

# mcp is the MCP SDK the server in mcpd/ imports. It comes from PyPI
# rather than the copr because it is a pure-Python library with no system
# side to package, and it lands in the same venv as liblore so the server
# and the code it calls are always the same generation.
uv pip install --python "$VENV_DIR/bin/python" --upgrade \
    "git+https://git.kernel.org/pub/scm/utils/liblore/liblore.git" \
    "git+https://git.kernel.org/pub/scm/utils/korgalore/korgalore.git" \
    "mcp"
