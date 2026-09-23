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
========================  =========  ==========================================

Where things live
-----------------
Everything is in the ``/data`` volume. You can edit these files by hand:

==============================================  ===============================
``/data/xdg/config/korgalore/korgalore.toml``   Which subsystems are tracked.
``/data/publicinbox/config``                    Which archives are served.
``/data/grokmirror/grokmirror.conf``            Which git trees are mirrored.
==============================================  ===============================

When you save one, the service that reads it picks up the change within a
few seconds. You don't need to restart anything. To get a shell::

    podman exec -it maint bash

``lei q`` inside the container searches every tracked subsystem.

Updating
--------
korgalore and liblore update themselves every day. For everything else,
rebuild the image from your git checkout. See :doc:`updating`.
