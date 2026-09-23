Updating
========
There are no ready-made images yet, so you update the container from
your git checkout. Your data is in the ``maint`` volume, and an update
never touches it.

.. note::

   We plan to publish an official image. When we do, updating will be
   simpler: you will pull the new image instead of building it
   yourself, and you won't need a git checkout.

What updates by itself
----------------------
Some things stay current without any work from you:

* Mail and git trees. The container fetches them all the time.
* korgalore and liblore. The container installs them again from git
  every time it starts, and once a day after that.

Everything else is inside the image: the dashboard, the MCP server,
public-inbox, lei, cgit, grokmirror, git, and the base system. To update
those, rebuild the image.

Update
------
1. Get the new version::

       cd maintainer-container
       git pull

   To see what changed::

       git log --oneline ORIG_HEAD..

2. Keep the current image, so you can go back to it if you need to::

       podman tag maintainer-container maintainer-container:previous

3. Rebuild the image::

       podman build --pull=newer --no-cache -t maintainer-container .

   Don't leave out ``--no-cache``. Without it, podman reuses the step
   that installed the packages, so public-inbox, cgit, grokmirror and
   the rest stay at their old versions. ``--pull=newer`` gets the new
   base image, if there is one. The build takes a few minutes.

4. Re-create the container from the new image::

       podman rm -f maint
       podman run -d --name maint -p 127.0.0.1:11043:11043 \
           -v maint:/data maintainer-container

   Use the same ``-p`` and ``-e`` options that you used the first time.

   With the quadlet (see :doc:`remote`), restart the service instead.
   It always starts from the newest image::

       systemctl --user restart maint

5. Check that it came back. Open http://127.0.0.1:11043/, or read the
   log::

       podman logs -f maint              # with podman run
       journalctl --user -u maint -f     # with the quadlet

6. When you are happy with the new version, delete the old image::

       podman rmi maintainer-container:previous
       podman image prune

If the quadlet changed
----------------------
Sometimes an update changes ``systemd/maint.container``. Your installed
copy is not updated for you, because it may have your own changes, such
as a VPN address. Compare the two::

    diff -u ~/.config/containers/systemd/maint.container systemd/maint.container

Copy over the changes you want, then run::

    systemctl --user daemon-reload
    systemctl --user restart maint

Go back to the previous version
-------------------------------
If the new version doesn't work, go back to the image you kept in
step 2::

    podman tag maintainer-container:previous maintainer-container:latest

Then do step 4 again. Your data stays as the new version left it.
Please tell us what went wrong at tools@kernel.org.

Start over
----------
To go back to a new install, use ``powerwash``. It removes your
settings, the mail archives, and the git trees you mirror. The next
start is like the first one: the dashboard asks for your email address
again, and the mail is downloaded again.

It keeps the git history that the trees share, because that is the
biggest download, about 4 GB for a kernel tree. When you choose a tree
again, only what is new since the powerwash is downloaded. If you don't
choose a tree again, the container removes its history after a day.

Stop the container first. ``powerwash`` refuses to run while the
container uses the volume::

    systemctl --user stop maint       # with the quadlet
    podman stop maint                 # with podman run

    podman run --rm -it -v maint:/data maintainer-container powerwash

It asks you to type ``powerwash`` before it removes anything. To skip
the question in a script, add ``--yes`` and leave out ``-it``.

To remove the git history too, add ``--remove-git``. Then the volume
is completely empty. To avoid the big download afterwards, run
:ref:`preload` before the next start.

Then start the container again::

    systemctl --user start maint      # with the quadlet
    podman start maint                # with podman run
