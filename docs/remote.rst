Running on another machine
==========================
Use this for a VM or a spare box that you reach over ssh.

Run it as a service
-------------------
On that machine, do the ``git clone`` and ``podman build`` steps from
:ref:`build`, but not ``podman run``: the service below starts the
container instead. Then, in the ``maintainer-container`` directory, the
``systemd/`` directory has a podman quadlet. It runs the container as a
systemd user service, called ``maint``::

    loginctl enable-linger $USER
    podman volume create maint
    mkdir -p ~/.config/containers/systemd
    cp systemd/maint.container ~/.config/containers/systemd/
    systemctl --user daemon-reload
    systemctl --user start maint

Don't skip ``enable-linger``. Without it, the container stops when you
log out, and the first start is long.

If the machine already has a Linux clone, run the ``preload``
command from :ref:`preload` after ``podman volume create`` and before
you start the service.

To read the log::

    journalctl --user -u maint -f

To change the unit, edit the copy in ``~/.config/containers/systemd/``,
then run ``systemctl --user daemon-reload``.

Start it at boot
~~~~~~~~~~~~~~~~
The service does not start at boot unless you ask for it. Add this to
the installed copy, then run ``systemctl --user daemon-reload``::

    [Install]
    WantedBy=default.target

Reach it with an ssh tunnel
---------------------------
The port stays on ``127.0.0.1`` on the remote machine. From your
workstation::

    ssh -N -L 11043:127.0.0.1:11043 you@yourbox

Then use http://127.0.0.1:11043/ as usual. Add one ``-L`` for each extra
port, such as ``11143`` for IMAP.

For a tunnel you use every day, put it in ``~/.ssh/config`` and run
``ssh -N maint``::

    Host maint
        HostName yourbox
        User you
        LocalForward 11043 127.0.0.1:11043
        ExitOnForwardFailure yes
        ServerAliveInterval 30

.. warning::

   Nothing in this container asks for a password: not the web pages, not
   the MCP server, not IMAP or NNTP. Never publish its ports on
   ``0.0.0.0`` or on a public address.

Reach it over a VPN
-------------------
On a tailnet or wireguard network, publish on the VPN address and tell
the dashboard its public URL. In the installed unit::

    PublishPort=100.x.y.z:11043:11043
    Environment=ROUTER_PUBLIC_BASE=http://<name>.<tailnet>.ts.net:11043
    Environment=PUBLISH_ADDRESS=100.x.y.z

Then run ``systemctl --user daemon-reload && systemctl --user restart maint``.

* Use the VPN interface's own address, never ``0.0.0.0``.
* Change both ``Environment`` lines. Without them, the dashboard shows
  URLs and ``-p`` flags for ``127.0.0.1``, which other machines can't
  reach.

If you use firewalld, trust the VPN interface once::

    sudo firewall-cmd --permanent --zone=trusted --add-interface=tailscale0
    sudo firewall-cmd --reload
