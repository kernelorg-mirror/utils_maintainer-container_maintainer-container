# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
"""Shared setup for the container's Python tests.

The MCP server and the setup dashboard read their paths and ports out of
the environment at import time and refuse to start without them, which is
what we want in the container -- a missing value there means defaults.env
was never sourced, and inventing a fallback would only move the failure
somewhere harder to read. It does mean the environment has to exist before the module is
imported, so it is set here rather than in a fixture: fixtures run after
collection, and collection is when the import happens.

The values are deliberately paths that do not exist. Nothing in the tests
runs lei or grok-pull, and a test that accidentally did should fail loudly
rather than quietly read the developer's own archive or mirror. A test that
needs a real directory monkeypatches the module constant instead.
"""

import os

os.environ.setdefault('DATA_DIR', '/nonexistent/data')
os.environ.setdefault('PI_CONFIG', '/nonexistent/data/publicinbox/config')
os.environ.setdefault('PUBLICINBOX_EXTINDEX_PATH', '/nonexistent/data/publicinbox/extindex')
os.environ.setdefault('KORGALORE_CONFD_PATH', '/nonexistent/data/xdg/config/korgalore/conf.d')
os.environ.setdefault('LEI_SAVED_SEARCHES_PATH', '/nonexistent/data/xdg/data/lei/saved-searches')
os.environ.setdefault('MCP_HOST', '127.0.0.1')
os.environ.setdefault('MCP_PORT', '11045')

# The setup dashboard (setup/app.py) reads these, and reads them all at
# import time for the same reason the MCP server does.
os.environ.setdefault('DASHBOARD_HOST', '127.0.0.1')
os.environ.setdefault('DASHBOARD_PORT', '11044')
os.environ.setdefault('ROUTER_PUBLIC_BASE', 'http://127.0.0.1:11043')
os.environ.setdefault('KGL', '/nonexistent/venv/bin/kgl')
os.environ.setdefault('DAEMONS_PATH', '/nonexistent/data/enabled-daemons.json')
os.environ.setdefault('PUBLISH_ADDRESS', '127.0.0.1')
os.environ.setdefault('IMAP_PORT', '11143')
os.environ.setdefault('NNTP_PORT', '11119')
os.environ.setdefault('EXTINDEX_STAMP_PATH', '/nonexistent/data/publicinbox/.extindex-stamp')
os.environ.setdefault('GROKMIRROR_DIR', '/nonexistent/data/grokmirror')
os.environ.setdefault('GROKMIRROR_TOPLEVEL', '/nonexistent/data/grokmirror/repos')
os.environ.setdefault('GROKMIRROR_OBJSTORE', '/nonexistent/data/grokmirror/objstore')
os.environ.setdefault('GROKMIRROR_PRELOAD_DIR', '/nonexistent/data/grokmirror/preload')
os.environ.setdefault('GROKMIRROR_SITE', 'https://git.kernel.org')
os.environ.setdefault('GROKMIRROR_CONF_PATH', '/nonexistent/data/grokmirror/grokmirror.conf')
os.environ.setdefault('GROKMIRROR_MANIFEST_PATH', '/nonexistent/data/grokmirror/manifest.js.gz')
os.environ.setdefault('GROKMIRROR_PID_PATH', '/nonexistent/data/grokmirror/grok-pull.pid')
os.environ.setdefault('GROKMIRROR_LOG_PATH', '/nonexistent/data/grokmirror/grok-pull.log')
os.environ.setdefault('GROKMIRROR_SOCKET_PATH', '/nonexistent/data/grokmirror/grok-pull.socket')
os.environ.setdefault('SYNC_REQUEST_PATH', '/nonexistent/data/sync-requested')
os.environ.setdefault('KGL_PULL_STATE_PATH', '/nonexistent/data/kgl-pull-state')
os.environ.setdefault('PULL_STAMP_PATH', '/nonexistent/data/lore-updated')
