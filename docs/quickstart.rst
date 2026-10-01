Quick start
===========
You need podman. The first start downloads git trees and mail, so it
takes a while, and the container needs disk space for your subsystems.

.. _build:

Get it and build it
-------------------
There are no ready-made images yet, so you build the image yourself.
Get the code::

    git clone https://git.kernel.org/pub/scm/utils/maintainer-container/maintainer-container.git
    cd maintainer-container

Build the image and start the container::

    podman build -t maintainer-container .
    podman run -d --name maint -p 127.0.0.1:11043:11043 \
        -v maint:/data maintainer-container

All data lives in the ``maint`` volume. You can remove and re-create the
container at any time without losing anything.

If the container runs on another machine, see :doc:`remote` first.

.. _preload:

Start from a Linux clone you already have
-----------------------------------------
A first mirror of a kernel tree downloads about 4 GB. Almost all of that
is old history, and any recent Linux clone already has it. It can be
mainline, a subsystem tree, or your own fork. Copy the objects from
your clone instead. Do this after ``podman build`` and before you choose
your trees::

    podman volume create maint
    podman run --rm --security-opt label=disable \
        -v maint:/data -v ~/linux:/seed:ro maintainer-container preload

Replace ``~/linux`` with the path to your clone. The clone is mounted
read-only, and it is not changed.

Only commits that kernel.org also has are copied, with their history,
so your own branches stay on your machine. This takes a few minutes.
After that, the mirror downloads only what your clone doesn't have, for any kernel
tree you choose.

Choose your subsystems
----------------------
Open http://127.0.0.1:11043/ and:

1. Enter your email address, the way it appears in ``MAINTAINERS``.
   Every subsystem that lists you as ``M:`` or ``R:`` is selected for you.
2. Add or remove subsystems, and choose which git trees to mirror.
3. Start the import, and wait for it to finish.

The last page shows the commands for your setup, ready to copy. The
sections below explain them. You can come back to that page at any time.

Fetch threads with b4
---------------------
Point b4 at the local archive::

    git config --global b4.midmask 'http://127.0.0.1:11043/lore/all/%s'
    git config --global b4.linkmask 'https://patch.msgid.link/%s'

``midmask`` is where b4 fetches threads from. ``linkmask`` builds the
``Link:`` trailers in your commits, so it stays public.

The local archive only has the subsystems you chose, and only goes back
as far as you imported. It tells b4 so, and names lore.kernel.org as
the place to ask for the rest. When a thread is not here, b4 fetches it
from lore.kernel.org on its own. The local archive does not keep a copy:
the next time you ask for that thread, b4 goes to lore.kernel.org again.

This needs liblore 0.10 or newer on your machine (b4 uses it to talk to
archives). With an older liblore, b4 reports the thread as missing. For
that thread, go back to lore.kernel.org::

    git config --global --unset b4.midmask

Fetch git from the local mirror
-------------------------------
In an existing clone, add the mirror as a remote::

    git remote add local http://127.0.0.1:11043/pub/scm/linux/kernel/git/torvalds/linux.git

Or fetch from the mirror and keep pushing to gitolite. Set the push URL
first::

    git remote set-url --push origin git@gitolite.kernel.org:pub/scm/linux/kernel/git/torvalds/linux
    git remote set-url origin http://127.0.0.1:11043/pub/scm/linux/kernel/git/torvalds/linux.git

Use your own tree's path instead of ``torvalds/linux``. Only the trees
you chose are mirrored. Other clones keep fetching from kernel.org.

Connect an LLM agent
--------------------
The MCP server is at ``http://127.0.0.1:11043/mcp`` and uses the
streamable HTTP transport. For example, with Claude Code::

    claude mcp add --transport http maintainer-container http://127.0.0.1:11043/mcp

A client that only speaks stdio can use a bridge::

    npx -y mcp-remote http://127.0.0.1:11043/mcp

The server returns who wrote what, when, and in which thread. For message
bodies it points the agent to ``b4 mbox``, so set up b4 first.

Read mail in a mail client
--------------------------
IMAP (port ``11143``) and NNTP (port ``11119``) are off by default. Turn
them on in the dashboard, then publish the port when you create the
container, for example ``-p 127.0.0.1:11143:11143``. The dashboard shows
the settings for your mail client.
