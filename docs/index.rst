maintainer-container
====================
A podman container that gives a kernel subsystem maintainer a local copy
of their part of lore.kernel.org and git.kernel.org. It keeps itself up
to date, and you set it up once in a browser.

Everything is on one port, ``11043``:

================================  ==========================================
``http://127.0.0.1:11043/``       The dashboard: choose what to track.
``http://127.0.0.1:11043/lore/``  The mail archive of your subsystems.
``http://127.0.0.1:11043/cgit/``  The git trees you mirror.
``http://127.0.0.1:11043/mcp``    An MCP server, for LLM agents.
================================  ==========================================

You can also ``git clone`` or ``git fetch`` from the same port. b4 can
fetch threads from it, and you can read the archive in a mail client over
IMAP or NNTP.

.. toctree::
   :maxdepth: 1

   quickstart
   remote
   updating
   reference

Getting help
------------
To report a problem or suggest a feature, please send plaintext email to
tools@kernel.org.
