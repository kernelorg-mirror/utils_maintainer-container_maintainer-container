"""Tests for the setup dashboard's coderepo wiring.

A `coderepo' link is what lets public-inbox reconstruct a blob out of an
emailed patch (PublicInbox::SolverGit), so these cover the two halves of
that: picking which mirrored repositories an inbox should be anchored to,
and writing the link out in a form git-config -- and therefore
PublicInbox::Config -- reads back.
"""

import shutil
import subprocess
from pathlib import Path
from typing import List

import pytest
from korgalore.maintainers import SubsystemEntry, Tree

import app

MAINLINE = 'pub/scm/linux/kernel/git/torvalds/linux.git'
USB = 'pub/scm/linux/kernel/git/gregkh/usb.git'
KSELFTEST = 'pub/scm/linux/kernel/git/shuah/linux-kselftest.git'


def make_repo(toplevel: Path, nick: str) -> Path:
    """Create what grok-pull leaves behind for a finished clone."""
    path = toplevel / nick
    (path / 'objects').mkdir(parents=True)
    (path / 'HEAD').write_text('ref: refs/heads/master\n', encoding='utf-8')
    return path


def make_subsystem(name: str, urls: List[str]) -> SubsystemEntry:
    entry = SubsystemEntry(name=name)
    entry.trees = [Tree(vcs='git', url=url, branch=None) for url in urls]
    return entry


def git_config_get_all(path: Path, key: str) -> List[str]:
    """Read a key back the way public-inbox does, repeats included."""
    result = subprocess.run(
        ['git', 'config', '-f', str(path), '--get-all', key],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.split('\n')[:-1] if result.returncode == 0 else []


class TestIsMirroredRepo:
    def test_populated_bare_repo(self, tmp_path: Path) -> None:
        assert app.is_mirrored_repo(make_repo(tmp_path, 'some/repo.git'))

    def test_missing_directory(self, tmp_path: Path) -> None:
        assert not app.is_mirrored_repo(tmp_path / 'nothing/here.git')

    def test_directory_grok_pull_has_only_created(self, tmp_path: Path) -> None:
        # grok-pull makes the directory before it has finished cloning into
        # it, so existence on its own is not enough to link.
        (tmp_path / 'half/way.git').mkdir(parents=True)
        assert not app.is_mirrored_repo(tmp_path / 'half/way.git')

    def test_objects_without_head(self, tmp_path: Path) -> None:
        (tmp_path / 'half/way.git/objects').mkdir(parents=True)
        assert not app.is_mirrored_repo(tmp_path / 'half/way.git')


class TestCoderepoNicks:
    @pytest.fixture(autouse=True)
    def mirror(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        toplevel = tmp_path / 'repos'
        monkeypatch.setattr(app, 'GROKMIRROR_TOPLEVEL', toplevel)
        monkeypatch.setattr(app, 'SUBSYSTEMS', {})
        return toplevel

    def test_subsystem_tree_comes_before_mainline(self, mirror: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A posted patch is based on whatever the subsystem is carrying, so
        # that tree is the likeliest place to find the pre-image blob.
        make_repo(mirror, MAINLINE)
        make_repo(mirror, USB)
        monkeypatch.setitem(
            app.SUBSYSTEMS, 'USB SUBSYSTEM', make_subsystem('USB SUBSYSTEM', [f'git://git.kernel.org/{USB}'])
        )
        assert app.coderepo_nicks('USB SUBSYSTEM') == [USB, MAINLINE]

    def test_unknown_subsystem_still_gets_mainline(self, mirror: Path) -> None:
        make_repo(mirror, MAINLINE)
        assert app.coderepo_nicks('NOT A SUBSYSTEM') == [MAINLINE]

    def test_unmirrored_repos_are_skipped(self, mirror: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Linking a repo that is not there yet would only make solver fail
        # slower; the next config write picks it up once grok-pull has it.
        make_repo(mirror, MAINLINE)
        monkeypatch.setitem(
            app.SUBSYSTEMS, 'USB SUBSYSTEM', make_subsystem('USB SUBSYSTEM', [f'git://git.kernel.org/{USB}'])
        )
        assert app.coderepo_nicks('USB SUBSYSTEM') == [MAINLINE]

    def test_nothing_mirrored_yet(self, mirror: Path) -> None:
        assert app.coderepo_nicks('NOT A SUBSYSTEM') == []

    def test_offsite_trees_are_ignored(self, mirror: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A T: line can point at github.com, which grokmirror here will
        # never have; tree_manifest_path drops it and mainline remains.
        make_repo(mirror, MAINLINE)
        monkeypatch.setitem(
            app.SUBSYSTEMS,
            'SOMETHING',
            make_subsystem('SOMETHING', ['https://github.com/someone/something.git']),
        )
        assert app.coderepo_nicks('SOMETHING') == [MAINLINE]

    def test_mainline_is_not_repeated_when_it_is_the_subsystem_tree(
        self, mirror: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_repo(mirror, MAINLINE)
        monkeypatch.setitem(
            app.SUBSYSTEMS,
            'THE REST',
            make_subsystem('THE REST', [f'https://git.kernel.org/{MAINLINE}']),
        )
        assert app.coderepo_nicks('THE REST') == [MAINLINE]

    def test_duplicate_tree_lines_collapse(self, mirror: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # MAINTAINERS routinely lists the same repo twice over git:// and
        # https://, which tree_manifest_path resolves to one path.
        make_repo(mirror, MAINLINE)
        make_repo(mirror, USB)
        monkeypatch.setitem(
            app.SUBSYSTEMS,
            'USB SUBSYSTEM',
            make_subsystem('USB SUBSYSTEM', [f'git://git.kernel.org/{USB}', f'https://git.kernel.org/{USB}/']),
        )
        assert app.coderepo_nicks('USB SUBSYSTEM') == [USB, MAINLINE]


class TestWritePublicinboxConfig:
    @pytest.fixture
    def config_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / 'publicinbox/config'
        monkeypatch.setattr(app, 'PUBLICINBOX_CONFIG_PATH', path)
        monkeypatch.setattr(app, 'PUBLICINBOX_DIR', path.parent)
        monkeypatch.setattr(app, 'PUBLICINBOX_EXTINDEX_PATH', tmp_path / 'publicinbox/extindex')
        monkeypatch.setattr(app, 'GROKMIRROR_TOPLEVEL', tmp_path / 'repos')
        monkeypatch.setattr(app, 'SUBSYSTEMS', {})
        monkeypatch.setattr(app, 'get_xdg_data_dir', lambda: tmp_path / 'xdg')
        # Building the index and registering it with lei are the two things
        # in here that shell out; neither has anything to do with the file.
        monkeypatch.setattr(app, 'build_extindex', lambda: False)

        (tmp_path / 'xdg/lei/usb_subsystem-mailinglist').mkdir(parents=True)
        (tmp_path / 'xdg/lei/usb_subsystem-patches').mkdir(parents=True)
        monkeypatch.setitem(
            app.SUBSYSTEMS,
            'USB SUBSYSTEM',
            make_subsystem('USB SUBSYSTEM', [f'git://git.kernel.org/{USB}']),
        )
        return path

    def test_coderepo_lines_are_readable_by_git_config(self, config_path: Path, tmp_path: Path) -> None:
        make_repo(tmp_path / 'repos', MAINLINE)
        make_repo(tmp_path / 'repos', USB)
        app.write_publicinbox_config(['USB SUBSYSTEM'])

        for section in ('usb_subsystem-mailinglist', 'usb_subsystem-patches'):
            assert git_config_get_all(config_path, f'publicinbox.{section}.coderepo') == [USB, MAINLINE]

    def test_repeated_section_accumulates_rather_than_replacing(self, config_path: Path, tmp_path: Path) -> None:
        # The coderepo stanza is written as a second [publicinbox "name"]
        # section, which only works because git-config merges them. If it
        # did not, the keys configparser wrote would be gone.
        make_repo(tmp_path / 'repos', MAINLINE)
        app.write_publicinbox_config(['USB SUBSYSTEM'])

        key = 'publicinbox.usb_subsystem-patches'
        assert git_config_get_all(config_path, f'{key}.inboxdir') == [str(tmp_path / 'xdg/lei/usb_subsystem-patches')]
        assert git_config_get_all(config_path, f'{key}.newsgroup') == ['local.maintainer.usb_subsystem.patches']
        assert git_config_get_all(config_path, f'{key}.coderepo') == [MAINLINE]

    def test_no_coderepo_when_nothing_is_mirrored(self, config_path: Path) -> None:
        # Before the first grok-pull finishes there is nothing to anchor
        # on, and an empty coderepo value would be worse than no key.
        app.write_publicinbox_config(['USB SUBSYSTEM'])
        assert git_config_get_all(config_path, 'publicinbox.usb_subsystem-patches.coderepo') == []
        assert '[publicinbox "usb_subsystem-patches"]' in config_path.read_text(encoding='utf-8')

    def test_css_lines_survive_the_coderepo_stanzas(self, config_path: Path, tmp_path: Path) -> None:
        make_repo(tmp_path / 'repos', MAINLINE)
        app.write_publicinbox_config(['USB SUBSYSTEM'])
        assert len(git_config_get_all(config_path, 'publicinbox.css')) == 2

    def test_extindex_gets_the_union(self, config_path: Path, tmp_path: Path) -> None:
        # /lore/all/<oid>/s/ is the one URL an agent can build from a blob
        # OID alone, so the extindex needs every subsystem's repos.
        make_repo(tmp_path / 'repos', MAINLINE)
        make_repo(tmp_path / 'repos', USB)
        app.write_publicinbox_config(['USB SUBSYSTEM'])
        assert git_config_get_all(config_path, 'extindex.all.coderepo') == [USB, MAINLINE]

    def test_extindex_topdir_survives_the_coderepo_stanza(self, config_path: Path, tmp_path: Path) -> None:
        make_repo(tmp_path / 'repos', MAINLINE)
        app.write_publicinbox_config(['USB SUBSYSTEM'])
        assert git_config_get_all(config_path, 'extindex.all.topdir') == [str(tmp_path / 'publicinbox/extindex')]

    def test_extindex_has_no_coderepo_when_nothing_is_mirrored(self, config_path: Path) -> None:
        app.write_publicinbox_config(['USB SUBSYSTEM'])
        assert git_config_get_all(config_path, 'extindex.all.coderepo') == []


class TestNoInboxUrl:
    """url= is deliberately absent from every section we write.

    PublicInbox::WwwListing links an inbox by url= when it is set and by a
    relative `name/' when it is not, so setting it pins the /lore/ front
    page to one address. This container is routinely reached at several.
    """

    @pytest.fixture
    def written(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / 'publicinbox/config'
        monkeypatch.setattr(app, 'PUBLICINBOX_CONFIG_PATH', path)
        monkeypatch.setattr(app, 'PUBLICINBOX_DIR', path.parent)
        monkeypatch.setattr(app, 'PUBLICINBOX_EXTINDEX_PATH', tmp_path / 'publicinbox/extindex')
        monkeypatch.setattr(app, 'GROKMIRROR_TOPLEVEL', tmp_path / 'repos')
        monkeypatch.setattr(app, 'SUBSYSTEMS', {})
        monkeypatch.setattr(app, 'get_xdg_data_dir', lambda: tmp_path / 'xdg')
        monkeypatch.setattr(app, 'build_extindex', lambda: False)
        (tmp_path / 'xdg/lei/kernel_selftest_framework-patches').mkdir(parents=True)
        monkeypatch.setitem(
            app.SUBSYSTEMS,
            'KERNEL SELFTEST FRAMEWORK',
            make_subsystem('KERNEL SELFTEST FRAMEWORK', [f'git://git.kernel.org/{KSELFTEST}']),
        )
        app.write_publicinbox_config(['KERNEL SELFTEST FRAMEWORK'])
        return path

    def test_no_inbox_carries_a_url(self, written: Path) -> None:
        key = 'publicinbox.kernel_selftest_framework-patches.url'
        assert git_config_get_all(written, key) == []

    def test_the_extindex_carries_no_url_either(self, written: Path) -> None:
        # /lore/all/ is listed on the same front page and links the same way.
        assert git_config_get_all(written, 'extindex.all.url') == []

    def test_the_inbox_is_still_declared(self, written: Path) -> None:
        # Dropping url must not drop the section: inboxdir is what makes it
        # an inbox at all.
        key = 'publicinbox.kernel_selftest_framework-patches.inboxdir'
        assert git_config_get_all(written, key) != []

    def test_nameisurl_keeps_the_inbox_listed(self, written: Path) -> None:
        # What goes wrong without it: WwwListing's hide_inbox matches the
        # wwwlisting pattern against the inbox's url= list, which is empty
        # once url= is gone, so /lore/ lists nothing but all/. nameIsUrl
        # supplies the `.' it matches instead.
        assert git_config_get_all(written, 'publicinbox.nameIsUrl') == ['true']

    def test_wwwlisting_is_still_all(self, written: Path) -> None:
        # nameIsUrl only helps while the pattern is the catch-all one:
        # WwwListing only substitutes `.' for the url list under
        # wwwlisting=all.
        assert git_config_get_all(written, 'publicinbox.wwwlisting') == ['all']

    def test_summary_still_reports_a_reachable_url(self, written: Path) -> None:
        # configured_inboxes falls back to ROUTER_PUBLIC_BASE, so what the
        # dashboard shows and offers to copy is unaffected.
        found = {inbox['name']: inbox['url'] for inbox in app.configured_inboxes()}
        assert found['kernel_selftest_framework-patches'] == (
            f'{app.ROUTER_PUBLIC_BASE}/lore/kernel_selftest_framework-patches/'
        )


class TestPublishHint:
    def test_the_hint_uses_the_configured_publish_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The default is loopback, matching the documented `podman run'.
        hints = {d['id']: d['publish'] for d in app.daemons_status()['daemons']}
        assert hints['imap'] == f'-p 127.0.0.1:{app.IMAP_PORT}:{app.IMAP_PORT}'

    def test_a_vpn_bind_is_reflected_in_the_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Publishing the router on a VPN address and being told to publish
        # IMAP on loopback would be no help: the hint would name an address
        # nothing on that network can reach.
        monkeypatch.setattr(app, 'PUBLISH_ADDRESS', '100.99.241.72')
        hints = {d['id']: d['publish'] for d in app.daemons_status()['daemons']}
        assert hints['imap'] == f'-p 100.99.241.72:{app.IMAP_PORT}:{app.IMAP_PORT}'
        assert hints['nntp'] == f'-p 100.99.241.72:{app.NNTP_PORT}:{app.NNTP_PORT}'


class TestSyncCoderepoLinks:
    """The reconciliation that a cold start depends on.

    The config is written while the subsystem's own tree is still
    cloning, so the first write anchors on mainline alone. Nothing else
    rewrites it afterwards, which is what this loop is for.
    """

    @pytest.fixture
    def cold(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / 'publicinbox/config'
        monkeypatch.setattr(app, 'PUBLICINBOX_CONFIG_PATH', path)
        monkeypatch.setattr(app, 'PUBLICINBOX_DIR', path.parent)
        monkeypatch.setattr(app, 'PUBLICINBOX_EXTINDEX_PATH', tmp_path / 'publicinbox/extindex')
        monkeypatch.setattr(app, 'GROKMIRROR_TOPLEVEL', tmp_path / 'repos')
        monkeypatch.setattr(app, 'SELECTION_PATH', tmp_path / 'selected-subsystems.json')
        monkeypatch.setattr(app, 'SUBSYSTEMS', {})
        monkeypatch.setattr(app, 'get_xdg_data_dir', lambda: tmp_path / 'xdg')
        monkeypatch.setattr(app, 'build_extindex', lambda: False)

        (tmp_path / 'xdg/lei/kernel_selftest_framework-mailinglist').mkdir(parents=True)
        (tmp_path / 'xdg/lei/kernel_selftest_framework-patches').mkdir(parents=True)
        monkeypatch.setitem(
            app.SUBSYSTEMS,
            'KERNEL SELFTEST FRAMEWORK',
            make_subsystem('KERNEL SELFTEST FRAMEWORK', [f'git://git.kernel.org/{KSELFTEST}']),
        )
        app.write_selection(['KERNEL SELFTEST FRAMEWORK'])
        return path

    def test_the_subsystem_tree_is_linked_once_its_clone_finishes(self, cold: Path, tmp_path: Path) -> None:
        # The cold start: mainline is there, the subsystem's tree is not.
        make_repo(tmp_path / 'repos', MAINLINE)
        app.write_publicinbox_config(app.read_selection())
        key = 'publicinbox.kernel_selftest_framework-patches.coderepo'
        assert git_config_get_all(cold, key) == [MAINLINE]

        # grok-pull finishes, and the next pass relinks.
        make_repo(tmp_path / 'repos', KSELFTEST)
        assert app.sync_coderepo_links() is True
        assert git_config_get_all(cold, key) == [KSELFTEST, MAINLINE]
        assert git_config_get_all(cold, 'extindex.all.coderepo') == [KSELFTEST, MAINLINE]

    def test_a_settled_config_is_left_alone(self, cold: Path, tmp_path: Path) -> None:
        # Rewriting on every pass would SIGHUP public-inbox once a minute
        # forever, since serve-web.sh watches the file's mtime.
        make_repo(tmp_path / 'repos', MAINLINE)
        make_repo(tmp_path / 'repos', KSELFTEST)
        app.write_publicinbox_config(app.read_selection())
        before = cold.stat().st_mtime_ns
        assert app.sync_coderepo_links() is False
        assert cold.stat().st_mtime_ns == before

    def test_nothing_to_do_before_the_first_config_exists(self, cold: Path, tmp_path: Path) -> None:
        # The dashboard's loop starts before setup does, so this is the
        # state it spends most of a fresh container's life in.
        make_repo(tmp_path / 'repos', MAINLINE)
        assert not cold.exists()
        assert app.sync_coderepo_links() is False
        assert not cold.exists()

    def test_links_are_dropped_when_a_repo_goes_away(self, cold: Path, tmp_path: Path) -> None:
        make_repo(tmp_path / 'repos', MAINLINE)
        make_repo(tmp_path / 'repos', KSELFTEST)
        app.write_publicinbox_config(app.read_selection())

        shutil.rmtree(tmp_path / 'repos' / KSELFTEST)
        assert app.sync_coderepo_links() is True
        assert git_config_get_all(cold, 'publicinbox.kernel_selftest_framework-patches.coderepo') == [MAINLINE]


class TestConfiguredCoderepos:
    def test_reads_back_exactly_what_was_planned(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The comparison sync_coderepo_links makes is only meaningful if
        # these two agree on their keys as well as their values.
        path = tmp_path / 'publicinbox/config'
        monkeypatch.setattr(app, 'PUBLICINBOX_CONFIG_PATH', path)
        monkeypatch.setattr(app, 'PUBLICINBOX_DIR', path.parent)
        monkeypatch.setattr(app, 'PUBLICINBOX_EXTINDEX_PATH', tmp_path / 'publicinbox/extindex')
        monkeypatch.setattr(app, 'GROKMIRROR_TOPLEVEL', tmp_path / 'repos')
        monkeypatch.setattr(app, 'SUBSYSTEMS', {})
        monkeypatch.setattr(app, 'get_xdg_data_dir', lambda: tmp_path / 'xdg')
        monkeypatch.setattr(app, 'build_extindex', lambda: False)
        (tmp_path / 'xdg/lei/kernel_selftest_framework-patches').mkdir(parents=True)
        monkeypatch.setitem(
            app.SUBSYSTEMS,
            'KERNEL SELFTEST FRAMEWORK',
            make_subsystem('KERNEL SELFTEST FRAMEWORK', [f'git://git.kernel.org/{KSELFTEST}']),
        )
        make_repo(tmp_path / 'repos', MAINLINE)
        make_repo(tmp_path / 'repos', KSELFTEST)

        names = ['KERNEL SELFTEST FRAMEWORK']
        app.write_publicinbox_config(names)
        assert app.configured_coderepos() == app.planned_coderepos(names)

    def test_no_config_file_reads_as_no_links(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app, 'PUBLICINBOX_CONFIG_PATH', tmp_path / 'nope')
        assert app.configured_coderepos() == {}


class TestSummary:
    """The finished-setup screen's payload.

    Only the parts a maintainer copies out of the page: if one of these
    goes missing the screen renders a blank code block, which reads as
    "nothing to do here" rather than as a bug.
    """

    @pytest.fixture(autouse=True)
    def quiet(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app, 'DATA_DIR', tmp_path)
        monkeypatch.setattr(app, 'SELECTION_PATH', tmp_path / 'selected-subsystems.json')
        monkeypatch.setattr(app, 'REPO_SELECTION_PATH', tmp_path / 'selected-repos.json')
        monkeypatch.setattr(app, 'IDENTITY_PATH', tmp_path / 'identity.json')
        monkeypatch.setattr(app, 'DAEMONS_PATH', tmp_path / 'enabled-daemons.json')
        monkeypatch.setattr(app, 'PUBLICINBOX_CONFIG_PATH', tmp_path / 'publicinbox/config')

    def test_mcp_url_goes_through_the_router(self) -> None:
        # Not MCP_PORT: that one is not published, and printing it would
        # only invite somebody to publish it.
        assert app.summary()['mcp']['url'] == f'{app.ROUTER_PUBLIC_BASE}/mcp'

    def test_the_copyable_endpoints_are_all_there(self) -> None:
        summary = app.summary()
        assert summary['b4']['midmask'].endswith('/lore/all/%s')
        assert summary['lore'] == f'{app.ROUTER_PUBLIC_BASE}/lore/'
        assert summary['mcp']['url'].startswith(app.ROUTER_PUBLIC_BASE)


class TestTrackSubsystemInvocation:
    """The argv `kgl track-subsystem' is run with.

    The mirror this builds is read by b4, which reassembles a patch series
    out of a whole thread. A query that matched single messages would fill
    it with fragments -- patch 3 of 7, a review with nothing to review --
    so the flag that pulls whole threads is part of the contract, not a
    tuning knob.
    """

    @pytest.fixture
    def argv(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> List[List[str]]:
        """Capture each command _run_generate launches, running none of them."""
        calls: List[List[str]] = []

        class FakeProc:
            stdout = iter(())

            def wait(self) -> int:
                return 0

        def fake_popen(cmd: List[str], **kwargs: object) -> FakeProc:
            calls.append(cmd)
            return FakeProc()

        monkeypatch.setattr(subprocess, 'Popen', fake_popen)
        monkeypatch.setattr(app, 'SUBSYSTEMS', {'PAGE CACHE': object()})
        monkeypatch.setattr(app, 'ensure_default_target', lambda: None)
        monkeypatch.setattr(app, 'write_publicinbox_config', lambda names: None)
        monkeypatch.setattr(app, 'DATA_DIR', tmp_path)
        app._run_generate(['PAGE CACHE'], '7.days.ago')
        return calls

    def test_whole_threads_are_pulled(self, argv: List[List[str]]) -> None:
        # Without this the archive holds orphan patches and b4 cannot
        # rebuild a series from it.
        assert '--threads' in argv[0]

    def test_the_subsystem_is_still_tracked_the_way_it_was(self, argv: List[List[str]]) -> None:
        cmd = argv[0]
        assert cmd[1] == 'track-subsystem'
        assert cmd[2] == 'PAGE CACHE'
        assert cmd[cmd.index('--since') + 1] == '7.days.ago'
