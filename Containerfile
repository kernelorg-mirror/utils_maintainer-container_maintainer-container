# One image with everything a maintainer's local lore needs: public-inbox
# and lei for mail, grokmirror and cgit for git trees, and korgalore (from
# the venv on /data) to tie them to the maintainer's MAINTAINERS entries.
# See README.md for how the pieces fit together.

FROM almalinux:10

# EPEL 10 covers cgit/public-inbox's non-lfit deps (highlight, Plack,
# Email::MIME, Crypt::CBC, Parse::RecDescent, spamassassin, ...).
#
# gcc-c++ is not optional, however much it looks like it should be. lei
# builds its Xapian glue just-ahead-of-time rather than shipping it
# prebuilt, and LEI.pm line 34 is a bare `use PublicInbox::XapHelperCxx',
# whose line 16 is top-level code:
#
#     my $cxx = which($ENV{CXX} // 'c++') // which('clang') //
#                                             die 'no C++ compiler';
#
# A die at module scope aborts at compile time, so without a compiler on
# PATH lei-daemon does not start at all -- it does not fall back. (The
# pure-Perl XapHelper.pm exists, but XapClient.pm refuses it for lei:
# `return if "@argv" =~ /\b-l\b/', and lei always passes -l.) PI_NO_CXX
# looks like the escape hatch and isn't; it lives inside cmd(), which can
# only run once the module has already loaded and already found a compiler.
# Upstream means this -- commit 22ae93b8, "lei will only use the C++
# version" -- so carrying a toolchain is the cost of lei, and a modest one
# next to the archives and git repos a maintainer mirrors anyway.
#
# Given we must carry the compiler, these two make it worth carrying:
#
#   xapian14-core-devel   ships xapian-core.pc. Without it pkg-config finds
#                         no xapian-core, XapHelperCxx::cmd() dies inside
#                         LEI.pm's eval, and the helper silently never
#                         builds -- the cache ends up holding a generated
#                         xap_helper.cpp and no binary. It also pulls in
#                         xapian14-core-libs 1.4.31 in place of AlmaLinux's
#                         xapian-core-libs 1.4.23, which is what the copr
#                         builds xapian14 for in the first place.
#   xapian14-bindings-perl  the SWIG Xapian.pm bindings. Search.pm prefers
#                         them -- `for my $x (($ENV{PI_XAPIAN} // 'Xapian'),
#                         'Search::Xapian')' -- and without them public-inbox
#                         quietly falls back to the older XS Search::Xapian.
#
# Neither is pulled in by public-inbox or lei: the RPMs declare no
# dependency on a compiler or on Xapian headers at all, so nothing but this
# list gets them here.
#
# Plack::Middleware::ReverseProxy isn't pulled in by anything else, but
# router.psgi wants it whenever the container sits behind a front-end proxy
# (the expected deployment: nginx or an ssh tunnel in front of 11043) --
# without it the X-Forwarded-* headers are ignored and every URL public-inbox
# generates points at http://127.0.0.1:11043/ instead of the real site.
RUN dnf install -y epel-release 'dnf-command(copr)' && \
    dnf copr enable -y icon/lfit && \
    dnf install -y --nodocs --setopt=install_weak_deps=False \
        cgit \
        public-inbox \
        lei \
        python-grokmirror \
        git \
        python3 \
        python3-pip \
        curl \
        gcc-c++ \
        uv \
        git-daemon \
        perl-Plack-Middleware-ReverseProxy \
        xapian14-bindings-perl \
        xapian14-core-devel \
    && dnf clean all

# --nodocs and no weak deps, above, take the image from 674MB to 589MB
# (measured before the xapian14 packages, which add 3MB on top).
# Nothing in here reads a man page or an /usr/share/doc tree, and the
# recommends are mostly rpm-build scaffolding (the *-srpm-macros family,
# redhat-rpm-config, annobin, systemtap-sdt-devel) pulled in behind gcc-c++,
# which is here to compile lei's Xapian glue, not to build packages. The
# rest are optional XS accelerators with pure-perl fallbacks. Verified after
# the fact: lei still imports and queries, public-inbox's WWW/Cgit/
# GitHTTPBackend still load, and cgit still runs.

# System-wide so it covers grokmirror's clones as well as anything git does
# on its own -- lets clones use a repo's bundle-URI advertisement instead of
# pulling the whole pack straight from the origin server.
RUN git config --system transfer.bundleURI true

# uv runs korgalore + liblore out of /data/venv (see sync-venv.sh) -- not
# packaged, so it can track liblore's pace independently of the copr.

# uv's cache lives in the image layer, /data/venv lives on the volume --
# different filesystems, so hardlinking never works. Skip straight to copy
# mode instead of warning about it on every sync.
ENV UV_LINK_MODE=copy

# korgalore's own config/state (targets, conf.d/*.toml, lei queries) must
# live on the volume too, not in the container's throwaway $HOME.
ENV XDG_CONFIG_HOME=/data/xdg/config
ENV XDG_DATA_HOME=/data/xdg/data

# liblore is pre-1.0 and moving fast as part of this same effort, so
# sync-venv.sh installs both straight from git rather than pinning to
# whatever's on PyPI. entrypoint.sh runs it once on every container start
# and then backgrounds venv-sync-loop.sh, which reruns it daily -- not
# baked in at build time, since /data is a volume and the real venv lives
# on the host side of it, not in this image layer.
COPY --chmod=0755 sync-venv.sh entrypoint.sh venv-sync-loop.sh grok-pull-loop.sh grok-fsck-loop.sh kgl-pull-loop.sh serve-web.sh serve-mcp.sh preload-objstore.sh powerwash.sh /usr/local/bin/

# The one copy of every path and port the scripts, the dashboard and the
# router all have to agree on. entrypoint.sh sources it with `set -a' and
# everything it starts inherits the result.
COPY defaults.env /usr/local/lib/maintainer-container/defaults.env

COPY cgitrc /etc/cgitrc

# public-inbox ships no default stylesheet of its own (unlike cgit's bundled
# cgit.css) -- these are the project's own CC0-1.0 example stylesheets from
# its contrib/css, wired in via publicinbox.css by write_publicinbox_config
# (setup/app.py).
COPY contrib/css/216dark.css contrib/css/216light.css /etc/public-inbox/css/

# The dashboard runs out of the venv (it imports korgalore's MAINTAINERS
# parsing), so it can't be a plain script on PATH. router.psgi lives
# alongside it -- it fronts that same app via proxy_to_dashboard, so keeping
# them colocated matches how they relate at runtime.
COPY setup/ /usr/local/lib/maintainer-setup/
COPY router.psgi /usr/local/lib/maintainer-setup/router.psgi
COPY mcpd/ /usr/local/lib/maintainer-setup/mcpd/

VOLUME /data

# The one port that always needs publishing, and only to localhost --
# router.psgi (run by serve-web.sh under public-inbox-netd) fronts the
# dashboard, cgit, git smart/dumb-HTTP clones, and the mail archive all
# from here. Publish it as `-p 127.0.0.1:11043:11043` and tunnel over ssh
# if you're not on the box. 11043 is leetspeak for LLORE -- "local lore".
EXPOSE 11043

# Ports live in the 11xxx range, last three digits echoing the service's
# own well-known port (see README, "Ports"): 11143 IMAP, 11119 NNTP, and
# 11418 for git://. IMAP and NNTP are off until the dashboard turns them on,
# and are published only if the maintainer asks podman for them, so they are
# not EXPOSEd. git:// is not implemented -- git-daemon is installed and
# ENABLE_GITD is the intended knob, but nothing reads it. When it is, note
# that git:// is anonymous and carries no auth of its own, so it stays opt-in.

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/data/venv/bin/python", "/usr/local/lib/maintainer-setup/app.py"]
