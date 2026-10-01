# maintainer-container

A podman image that gets a new kernel subsystem maintainer running
korgalore, public-inbox, grokmirror, and cgit locally, self-updating, with
minimal setup. Its short name, LLORE, stands for "local lore", and its
port, `11043`, spells it. The guide for maintainers using it is in
`docs/`; this README is about how it works and how to work on it. The
reasons behind the less obvious choices are in comments next to the code
they explain.

## Status

Base image + upstream packages installed, plus korgalore and liblore in a
`uv`-managed venv at `/data/venv` (installed live from git, not PyPI, since
liblore is pre-1.0 and moving fast). The venv is synced on every container
start and again daily thereafter; override `VENV_SYNC_INTERVAL` (seconds)
to change the cadence. korgalore's own config and state (targets,
`conf.d/*.toml`, lei queries) live under `/data/xdg` on the volume too.

Everything is served from a single port, `:11043`, by one
`public-inbox-netd` process running a custom PSGI router (`router.psgi`)
instead of a plain reverse proxy in front of separate services:

- `/` -- the dashboard: opens by asking for the maintainer's email address
  as it appears in `MAINTAINERS`, which pre-selects every subsystem that
  lists them as `M:` or `R:`, then walks them through picking any others
  and running `kgl track-subsystem` for each one, seeding a no-op dummy
  target automatically so this works with no delivery-account setup and
  without duplicating mail storage -- the lei v2 archive underneath the
  feeds is already all the storage browse mode needs. The address itself
  is saved to `/data/identity.json`, which is where anything else on the
  volume looks to answer "which of these messages are mine?". It's
  also where root-level `git clone` URLs are served from, once a repo is
  mirrored (see below).
- `/cgit/` -- cgit browsing of every mirrored repo. grokmirror's shared
  object storage is kept in `/data/grokmirror/objstore`, next to the
  browsable toplevel rather than inside it (`GROKMIRROR_OBJSTORE`), so
  those repos -- objects with no history of their own -- stay out of both
  the cgit index and the clone paths below.
- `/lore/` -- the public-inbox mail archive, one inbox per tracked
  subsystem; `/lore/all/` is the cross-subsystem view backed by
  public-inbox's extindex. That extindex is also registered as a lei
  external, so `lei q` inside the container searches every tracked
  subsystem at once -- that is the local search `b4` and the MCP server
  query, with no request leaving the machine.
- `/mcp` -- the MCP server (`mcpd/server.py`), so an agent can search the
  archive and read a thread. A search can be narrowed to one tracked
  subsystem, which is a different and much smaller archive rather than a
  filter over the big one: korgalore delivers each subsystem's mail into
  its own v2 archive under `LEI_ARCHIVES_PATH`, indexed the same way the
  extindex is. A day of the whole archive is hundreds of messages; a day
  of one subsystem is single digits. Point a client at `http://127.0.0.1:11043/mcp`; it speaks
  streamable HTTP rather than stdio, because the container is as likely
  to be on a VM as on the laptop and stdio only ever reaches the machine
  it runs on. There is no authentication: what it serves is the same
  public archive `/lore/` already serves, and the protection is the same
  one everything else here relies on -- publish the port to `127.0.0.1`
  and forward it, don't expose it to the internet.

## Build

```shell
git clone https://git.kernel.org/pub/scm/utils/maintainer-container/maintainer-container.git
cd maintainer-container
podman build -t maintainer-container .
```

## Tests

```shell
./ci.sh
```

Runs the same gates in the same order as korgalore's `ci.sh`, stopping at
the first failure: `uv sync`, `ruff format --check`, `ruff check`, `ty`,
`mypy`, `pyright`, `pytest`. Each step has its own exit code (10 through
16) so a failure is identifiable from the exit status alone. To run one
gate on its own, `uv run pytest` and friends work as usual.

The Python in here is run by path inside the container rather than
installed, so `pythonpath`, `mypy_path`, pyright's `extraPaths` and ty's
`extra-paths` all name `mcpd` and `setup` -- that is how a checker
resolves `import archive` the way the container's `sys.path` does.
liblore *and* korgalore come from git, matching `sync-venv.sh`: without
korgalore installed, every `korgalore` import in `setup/app.py` reads as a
missing stub.

The Python tests need nothing but `uv`. `tests/test_router.py` is the
exception: it starts a real `public-inbox-netd` serving `router.psgi`
against a stub backend, because the router is Perl and cannot be imported
into pytest. It skips, naming what is missing, unless the host has

- `public-inbox` (for `public-inbox-netd`), and
- `perl-Plack-Middleware-ReverseProxy`

Both are in the container image already; this is only about running the
tests from a checkout. There is no CI for the Perl side, so a skip here is
a test nobody is running -- worth installing the two packages rather than
living with.

## Docs

The maintainer-facing guide lives in `docs/` (Sphinx). To build it:

```shell
uv run --no-project --with-requirements docs/requirements.txt \
    sphinx-build -W -b html docs docs/_build/html
```

## Run

```shell
podman run --rm -p 127.0.0.1:11043:11043 -v maint:/data maintainer-container
```

`11043` is leetspeak for `LLORE` -- "local lore". It's the port inside the
container as well as out, so the mapping is 1:1 and nothing has to be
translated when reading logs or URLs. Override it with `ROUTER_PORT` if it
collides with something you already have listening.

Then open <http://127.0.0.1:11043/> for the dashboard, which is where you
pick the subsystems you maintain. If the container isn't on your
workstation, reach it with `ssh -L 11043:127.0.0.1:11043 <host>` rather
than publishing the port more widely.

Once repos are mirrored, the same port serves smart-HTTP clones at a path
mirroring upstream's own layout, e.g.
`git clone http://127.0.0.1:11043/pub/scm/linux/kernel/git/torvalds/linux.git`.

### Preloading from a local Linux clone

A first mirror of a kernel tree is a ~4 GB download, nearly all of it
history that any recent Linux clone already has -- mainline, a subsystem
tree or a personal fork all work. Seed the object store from one before
picking any trees:

```shell
podman volume create maint
podman run --rm --security-opt label=disable \
    -v maint:/data -v ~/linux:/seed:ro maintainer-container preload
```

`preload-objstore.sh` borrows the clone as an alternate and uses
`pack-objects` to copy out the history behind mainline's own commits --
every branch head and tagged commit kernel.org advertises that the
clone's refs can reach -- so private branches stay behind, and the
clone's ref names don't matter. The result waits in
`/data/grokmirror/preload/` until the dashboard's first pull of a tree in
mainline's forkgroup, which renames it into
`/data/grokmirror/objstore/<forkgroup>.git`. grok-pull then fetches only
what the clone was missing. `label=disable` lets an SELinux host read the
clone without relabelling it.

### Starting over

`powerwash` empties the volume, so the next start is a first start:

```shell
podman stop maint
podman run --rm -it -v maint:/data maintainer-container powerwash
podman start maint
```

Everything goes, the venv included, except grokmirror's object stores
and any waiting preloads: git history is the 4 GB part, and it is the
same history after a powerwash as before. The repos that use it go, so
nothing counts as mirrored; a tree picked again finds its object store
already there and fetches only what is new. An object store nothing is
picked for again is dropped by grok-fsck a day later, as for any
deselected tree. `--remove-git` removes the history too.

It runs in the image rather than on the host so it works without a
checkout, which means it cannot stop the container itself. Instead the
entrypoint holds a `flock` on `/data/.lock` for as long as the container
runs, and `powerwash.sh` refuses to touch anything unless it can take
that lock. A container started during a powerwash waits for it to
finish. It asks before removing anything; `--yes` skips the question.

## Running it as a service

The `podman run` above is fine for a laptop you are sitting in front of.
On a machine you reach over ssh -- a VM, a spare box -- use the quadlet in
`systemd/` instead, which makes this a systemd user service you can
start, stop and read the log of. It is not started at login or boot; to
have it come back after a reboot, see "Starting it at boot" below:

```shell
podman volume create maint
mkdir -p ~/.config/containers/systemd
cp systemd/maint.container ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start maint
```

`journalctl --user -u maint -f` for the log. Edit the copy under
`~/.config/containers/systemd/` to publish more ports or pin an interval;
`systemctl --user daemon-reload` regenerates the service from it.

One step is easy to skip and expensive to skip:

```shell
loginctl enable-linger $USER
```

A user systemd instance normally exits with your last session, taking the
container with it. The first cold start clones the subsystem's git trees
and backfills the archives, which is not something to restart because an
ssh connection dropped.

Everything else about a remote one is the same as a local one: the port
stays on `127.0.0.1` and you reach it through `ssh -L`.

### Starting it at boot

The unit has no `[Install]` section on purpose: a full mirror plus lei and
Xapian indexing is a lot to bring up on a laptop that did not ask for it,
so `systemctl --user start maint` is the only thing that starts it. On a
box that should come back by itself, add this to the installed copy under
`~/.config/containers/systemd/` and run `systemctl --user daemon-reload`:

```ini
[Install]
WantedBy=default.target
```

### Reaching it over a VPN instead

An ssh forward is one way in; a tailnet or wireguard address is another,
and more convenient if several people or machines want the same archive.
Edit the installed copy of the unit -- not the one in the repo, since the
address belongs to the machine and not to the project:

```ini
PublishPort=100.x.y.z:11043:11043
Environment=ROUTER_PUBLIC_BASE=http://<name>.<tailnet>.ts.net:11043
Environment=PUBLISH_ADDRESS=100.x.y.z
```

then `systemctl --user daemon-reload && systemctl --user restart maint`.

Two things about that are easy to get wrong:

- **Publish on the VPN interface's own address, not `0.0.0.0`.** Nothing
  in this container authenticates anything -- not the archive, not the
  MCP endpoint -- and `0.0.0.0` also binds whatever public interface the
  machine has, leaving the firewall as the only thing in the way. Naming
  the VPN address gives the same reachability with nothing else reachable.
- **Both environment lines have to change with it.** The archive's own
  pages follow whatever address the request arrived on, so they are fine
  either way -- these two are what the dashboard tells you. Left at the
  default, `ROUTER_PUBLIC_BASE` makes it show and offer to copy a URL on
  the reader's own loopback, and `PUBLISH_ADDRESS` makes the ready-made
  `-p` flag for IMAP or NNTP bind an address nobody else can reach. They
  are two settings rather than one because `ROUTER_PUBLIC_BASE` is a URL
  and podman will not take a hostname in `--publish`.

The firewall still applies to the VPN interface. With firewalld, put that
interface in the trusted zone once:

```shell
sudo firewall-cmd --permanent --zone=trusted --add-interface=tailscale0
sudo firewall-cmd --reload
```

Leave the default zone alone: that is what keeps the public interface
closed.


## Settings

Every path, port and interval the container's parts have to agree on lives
in `defaults.env`, installed at
`/usr/local/lib/maintainer-container/defaults.env`. `entrypoint.sh` sources
it and exports the result, so the shell loops, the Python dashboard and the
Perl router all read the same values instead of each keeping its own copy.

Every entry is a `:=` default, so anything already set in the environment
wins -- `podman run -e KGL_PULL_INTERVAL=60 ...` overrides that one and
leaves the rest alone. To run `setup/app.py` or `router.psgi` outside the
container, source the file first:

```shell
set -a; . ./defaults.env; set +a
```

The one entry that is a plain `=` default is `ARCHIVE_UPSTREAM`, so that
`-e ARCHIVE_UPSTREAM=` can set it to empty -- see the next section.

## A partial archive

The archive under `/lore` only has the subsystems you track, so a 404 from
it, or a search that finds nothing, doesn't mean the mail doesn't exist.
Every `/lore` answer says so in two headers:

```
X-Archive-Coverage: partial; updated=1790000000
X-Archive-Upstream: https://lore.kernel.org/all/
```

A client that knows them (liblore, and so b4) asks the upstream for what
isn't here. `updated` is when the last good mail update started
(`kgl-pull-loop.sh` writes it to `/data/lore-updated`). For a thread the
archive has, the client can trust "nothing new since then" without asking
upstream. The stamp only moves when `kgl pull --fail-on-feed-error`
succeeded, the `all` index is current, and every served archive is one
korgalore updates (`setup/lore_coverage.py`). Otherwise it stays where it
was, and clients just ask upstream more often.

Why headers, and not a router that asks lore.kernel.org itself? The router
runs in a few blocking workers, and a few slow calls to lore would stall
the archive, cgit, git clones and the dashboard all at once. The client
already has its own failover, User-Agent and offline handling, and it
knows where each answer came from.

Set `ARCHIVE_UPSTREAM=` (empty) to send neither header, and look like a
full archive again.

## Ports

Everything this container listens on lives in the `11xxx` range, with the
last three digits echoing the service's own well-known port, so a number
is recognizable on sight and can't collide with the real service running
on the same host:

| port    | service            | exposed? |
|---------|--------------------|----------|
| `11043` | HTTP (`LLORE`)     | yes -- the one port that always needs publishing |
| `11044` | dashboard app      | no -- bound to `127.0.0.1` inside the container, reached only via the router's reverse proxy |
| `11045` | MCP server         | no -- same, reached at `/mcp` on `11043` |
| `11143` | IMAP (143)         | opt-in -- off until the dashboard turns it on, and published only if you asked podman for it |
| `11119` | NNTP (119)         | opt-in -- same |
| `11418` | `git://` (9418)    | planned, not implemented |

`11043` is the odd one out: it's leetspeak for `LLORE` ("local lore")
rather than a mangled 80, since this container's whole point is a local
lore. It's the port inside the container as well as out, so the mapping
is 1:1 and nothing needs translating when reading logs or URLs. Override
it with `ROUTER_PORT` if it collides with something you already have.

IMAP and NNTP are served by the same `public-inbox-netd` process that
serves the web port, so turning one on adds a listener rather than
starting a second daemon (`serve-web.sh`). They are off until the
dashboard writes the choice to `DAEMONS_PATH`, and enabling one only binds
it *inside* the container -- publishing it is podman's job and has to be
asked for when the container is created:

```sh
podman run -d --name maint \
    -p 127.0.0.1:11043:11043 \
    -p 127.0.0.1:11143:11143 \
    -p 127.0.0.1:11119:11119 \
    -v maint:/data maintainer-container:latest
```

`git://` has no code behind it yet -- `git-daemon` is installed and
`ENABLE_GITD` is mentioned in the Containerfile, but nothing reads that
variable, so setting it currently does nothing.

### Reaching it from another machine

Nothing here authenticates anybody: public-inbox's IMAP accepts any
credentials or none, NNTP has no `AUTHINFO` at all, and the web port
serves whatever it mirrors to whoever asks. So bind to `127.0.0.1` on the
container host, as above, and reach it over ssh rather than opening the
ports up.

One `-L` per port:

```sh
ssh -N \
    -L 11043:127.0.0.1:11043 \
    -L 11143:127.0.0.1:11143 \
    -L 11119:127.0.0.1:11119 \
    you@yourbox
```

`-N` means "no remote command, just hold the tunnels open" -- drop it if
you want a shell in the same connection. The `127.0.0.1` in each forward
is resolved on the *remote* side, which is exactly where podman published
the ports.

For a tunnel you leave up all day, three more options earn their keep:

```sh
ssh -f -N \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -L 11043:127.0.0.1:11043 \
    -L 11143:127.0.0.1:11143 \
    -L 11119:127.0.0.1:11119 \
    you@yourbox
```

`-f` backgrounds it once the forwards are up. `ExitOnForwardFailure=yes`
makes ssh fail loudly if a local port is already taken, instead of coming
up with that one forward missing and leaving you to puzzle over a
"connection refused" much later. `ServerAliveInterval=30` keeps an idle
tunnel from being quietly dropped by a NAT or firewall.

Or write it down once in `~/.ssh/config` and just type `ssh maint`:

```
Host maint
    HostName yourbox
    User you
    LocalForward 11043 127.0.0.1:11043
    LocalForward 11143 127.0.0.1:11143
    LocalForward 11119 127.0.0.1:11119
    ExitOnForwardFailure yes
    ServerAliveInterval 30
```

If the container sits behind nginx or another front-end proxy instead,
`router.psgi` honours the `X-Forwarded-*` headers
(`Plack::Middleware::ReverseProxy`), so the URLs public-inbox generates
will point at the real site rather than at `127.0.0.1`.
