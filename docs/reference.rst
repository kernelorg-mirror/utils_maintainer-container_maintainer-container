Reference
=========

Ports
-----
The last three digits match the usual port of each service.

=========  ================  ==============================================
Port       Service           Published?
=========  ================  ==============================================
``11043``  HTTP              Always. Everything else is reached through it.
``11143``  IMAP              Only if you turn it on and publish it.
``11119``  NNTP              Only if you turn it on and publish it.
=========  ================  ==============================================

``11043`` spells ``LLORE``, for "local lore". The same port is used inside
and outside the container. To change it, set ``ROUTER_PORT``.

Settings
--------
Set these with ``-e NAME=value`` on ``podman run``, or with
``Environment=NAME=value`` in the quadlet. The full list, with comments,
is in ``defaults.env``.

========================  =========  ==========================================
Setting                   Default    What it does
========================  =========  ==========================================
``ROUTER_PORT``           ``11043``  The web port.
``ROUTER_PUBLIC_BASE``    loopback   The URL the dashboard tells you to use.
``PUBLISH_ADDRESS``       loopback   The address in the ``-p`` flags it shows.
``KGL_PULL_INTERVAL``     ``600``    Seconds between mail updates.
``VENV_SYNC_INTERVAL``    ``86400``  Seconds between korgalore/liblore updates.
``WEB_WORKERS``           ``4``      Web server worker processes.
``ARCHIVE_UPSTREAM``      lore       Where to find the mail that isn't here.
========================  =========  ==========================================

The archive only has the subsystems you track. So every answer under
``/lore`` says that it is partial, and names ``ARCHIVE_UPSTREAM`` as the
place that has the rest. b4 then gets a thread that isn't here from
lore.kernel.org on its own. Set ``ARCHIVE_UPSTREAM=`` (empty) to turn this
off.

Where things live
-----------------
Everything is in the ``/data`` volume. You can edit these files by hand:

==============================================  ===============================
``/data/xdg/config/korgalore/korgalore.toml``   Which subsystems are tracked.
``/data/publicinbox/config``                    Which archives are served.
``/data/grokmirror/grokmirror.conf``            Which git trees are mirrored.
==============================================  ===============================

When you save one, the service that reads it picks up the change within a
few seconds. You don't need to restart anything.

``/data/lore-updated`` is the time the last good mail update started. The
archive sends it to b4, so b4 can trust "no new mail" from here. Don't edit
it: the container only moves it when every subsystem updated without an
error.

To get a shell::

    podman exec -it maint bash

``lei q`` inside the container searches every tracked subsystem.

Updating
--------
korgalore and liblore update themselves every day. For everything else,
rebuild the image from your git checkout. See :doc:`updating`.

Source code
-----------
The source code is at
https://git.kernel.org/pub/scm/utils/maintainer-container/maintainer-container.git.
