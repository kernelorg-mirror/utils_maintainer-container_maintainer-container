# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
project = 'maintainer-container'
copyright = '2026, The Linux Foundation and contributors'
author = 'Kernel.org'
html_theme = 'sphinx_rtd_theme'
html_static_path = ['_static']
# A 400px copy of the logo: the sidebar shows it at about 200px, so this is
# sharp on a high-DPI screen at a fraction of the original's size.
html_logo = '_static/llore-logo.png'
html_favicon = '_static/favicon.ico'
# Only the logo in the sidebar: "maintainer-container" is too long to sit
# beside it. The official name stays in every page title and in the
# heading of the front page.
html_theme_options = {'logo_only': True}
# Don't highlight by default
highlight_language = 'none'
master_doc = 'index'
