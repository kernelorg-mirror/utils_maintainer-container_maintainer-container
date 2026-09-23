"""Tests for seeding grokmirror's object store from a maintainer's own clone.

Two halves: preload-objstore.sh, which builds an object store out of a
clone, and adopt_preloads(), which moves one into place when the dashboard
starts a mirror. The script is run for real, against an "upstream" that is
just a bare repo on disk -- GROKMIRROR_SITE is prepended to the repo path,
so a directory works as well as https://git.kernel.org does.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Set

import pytest

import app

SCRIPT = Path(__file__).parent.parent / 'preload-objstore.sh'
UPSTREAM = '/pub/scm/demo/demo.git'


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.org', *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f'{name}\n', encoding='utf-8')
    git('add', name, cwd=repo)
    git('commit', '-q', '-m', name, cwd=repo)
    return git('rev-parse', 'HEAD', cwd=repo)


def has_object(repo: Path, oid: str) -> bool:
    result = subprocess.run(['git', 'cat-file', '-e', oid], cwd=repo, capture_output=True, check=False)
    return result.returncode == 0


class Seeded:
    """An upstream, a maintainer's clone of it, and what they know about both."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.site = tmp_path / 'site'
        self.upstream = self.site / UPSTREAM.lstrip('/')
        self.seed = tmp_path / 'clone'
        self.data = tmp_path / 'data'
        self.dest = self.data / 'preload' / UPSTREAM.lstrip('/')

        # Upstream: some history, both kinds of tag, and a second branch.
        work = tmp_path / 'upstream-work'
        work.mkdir()
        git('init', '-q', '-b', 'master', cwd=work)
        self.v1 = commit(work, 'one')
        git('tag', 'v1', cwd=work)
        self.v2 = commit(work, 'two')
        git('tag', '-a', '-m', 'v2', 'v2', cwd=work)
        self.v3 = commit(work, 'three')
        git('tag', 'v3', cwd=work)
        git('checkout', '-q', '-b', 'topic', 'v1', cwd=work)
        self.topic = commit(work, 'topic')
        git('checkout', '-q', 'master', cwd=work)
        self.upstream.parent.mkdir(parents=True)
        git('clone', '-q', '--bare', str(work), str(self.upstream), cwd=tmp_path)
        # So the partial-clone test can make one.
        git('config', 'uploadpack.allowFilter', 'true', cwd=self.upstream)

        # The maintainer's clone, which also has things nobody else does:
        # work in progress, and a tag of their own.
        self.clone(self.seed)
        git('checkout', '-q', '-b', 'wip', 'v2', cwd=self.seed)
        self.private = commit(self.seed, 'secret')
        git('tag', 'mine', cwd=self.seed)

    def clone(self, where: Path, *args: str) -> None:
        git('clone', '-q', *args, f'file://{self.upstream}', str(where), cwd=self.tmp_path)

    def env(self) -> Dict[str, str]:
        return {
            **os.environ,
            # Whichever python3 runs these tests has grokmirror (it is a dev
            # dependency); a system one may not.
            'PATH': f'{Path(sys.executable).parent}{os.pathsep}{os.environ["PATH"]}',
            'PRELOAD_SEED': str(self.seed),
            'GROKMIRROR_PRELOAD_DIR': str(self.data / 'preload'),
            'GROKMIRROR_OBJSTORE': str(self.data / 'objstore'),
            'GROKMIRROR_SITE': str(self.site),
        }

    def preload(self, *args: str) -> 'subprocess.CompletedProcess[str]':
        return subprocess.run(
            [str(SCRIPT), *args],
            env=self.env(),
            capture_output=True,
            text=True,
            check=False,
        )

    def refs(self) -> Dict[str, str]:
        out = git('for-each-ref', '--format=%(refname) %(objectname)', cwd=self.dest)
        return dict(line.split(' ') for line in out.splitlines())

    def starting_points(self) -> Set[str]:
        return set(self.refs().values())


@pytest.fixture
def seeded(tmp_path: Path) -> Seeded:
    return Seeded(tmp_path)


class TestPreloadScript:
    def test_upstream_commits_the_clone_has_are_the_starting_points(self, seeded: Seeded) -> None:
        result = seeded.preload(UPSTREAM)
        assert result.returncode == 0, result.stderr
        assert seeded.starting_points() == {seeded.v1, seeded.v2, seeded.v3, seeded.topic}

    def test_refs_are_named_after_their_objects(self, seeded: Seeded) -> None:
        seeded.preload(UPSTREAM)
        for ref, oid in seeded.refs().items():
            assert ref == f'refs/virtual/preload/{oid}'

    def test_nothing_private_comes_along(self, seeded: Seeded) -> None:
        seeded.preload(UPSTREAM)
        assert has_object(seeded.dest, seeded.v2)
        assert not has_object(seeded.dest, seeded.private)

    def test_a_clone_fetched_without_tags_works_just_as_well(self, seeded: Seeded, tmp_path: Path) -> None:
        seeded.seed = tmp_path / 'no-tags'
        seeded.clone(seeded.seed, '--no-tags')
        assert git('tag', cwd=seeded.seed) == ''
        result = seeded.preload(UPSTREAM)
        assert result.returncode == 0, result.stderr
        assert seeded.starting_points() == {seeded.v1, seeded.v2, seeded.v3, seeded.topic}

    def test_an_upstream_commit_the_clone_cannot_reach_is_left_out(self, seeded: Seeded) -> None:
        # Still in the clone's packs, but no ref leads to it any more -- the
        # state in which git stops promising that its history is all there.
        git('update-ref', '-d', 'refs/remotes/origin/topic', cwd=seeded.seed)
        assert has_object(seeded.seed, seeded.topic)
        result = seeded.preload(UPSTREAM)
        assert result.returncode == 0, result.stderr
        assert seeded.starting_points() == {seeded.v1, seeded.v2, seeded.v3}
        assert not has_object(seeded.dest, seeded.topic)

    def test_the_result_stands_on_its_own(self, seeded: Seeded) -> None:
        seeded.preload(UPSTREAM)
        assert not (seeded.dest / 'objects' / 'info' / 'alternates').exists()
        git('fsck', '--connectivity-only', '--no-dangling', cwd=seeded.dest)

    def test_it_is_set_up_the_way_grokmirror_sets_up_an_object_store(self, seeded: Seeded) -> None:
        seeded.preload(UPSTREAM)
        assert git('config', 'extensions.preciousObjects', cwd=seeded.dest) == 'true'
        assert git('config', '--get-all', 'pack.island', cwd=seeded.dest) == 'refs/virtual/([0-9a-f]+)/'
        assert (seeded.dest / 'grokmirror.objstore').exists()

    def test_it_leaves_nothing_else_behind(self, seeded: Seeded) -> None:
        seeded.preload(UPSTREAM)
        assert sorted(p.name for p in seeded.dest.parent.iterdir()) == ['demo.git']

    def test_mainline_is_the_default(self, seeded: Seeded) -> None:
        result = seeded.preload()
        assert result.returncode != 0
        assert '/pub/scm/linux/kernel/git/torvalds/linux.git' in result.stderr

    def test_a_second_run_changes_nothing(self, seeded: Seeded) -> None:
        seeded.preload(UPSTREAM)
        packs = sorted((seeded.dest / 'objects' / 'pack').iterdir())
        result = seeded.preload(UPSTREAM)
        assert result.returncode == 0
        assert 'nothing to do' in result.stderr
        assert sorted((seeded.dest / 'objects' / 'pack').iterdir()) == packs

    def test_an_unrelated_clone_gives_nothing(self, seeded: Seeded, tmp_path: Path) -> None:
        other = tmp_path / 'other'
        other.mkdir()
        git('init', '-q', cwd=other)
        commit(other, 'unrelated')
        git('tag', 'v1', cwd=other)
        seeded.seed = other
        result = seeded.preload(UPSTREAM)
        assert result.returncode != 0
        assert 'none of the history' in result.stderr
        assert not seeded.dest.parent.exists() or not any(seeded.dest.parent.iterdir())

    def test_a_shallow_clone_is_refused(self, seeded: Seeded, tmp_path: Path) -> None:
        seeded.seed = tmp_path / 'shallow'
        seeded.clone(seeded.seed, '--depth=1')
        result = seeded.preload(UPSTREAM)
        assert result.returncode != 0
        assert 'shallow clone' in result.stderr

    def test_a_partial_clone_is_refused(self, seeded: Seeded, tmp_path: Path) -> None:
        seeded.seed = tmp_path / 'partial'
        seeded.clone(seeded.seed, '--filter=blob:none')
        result = seeded.preload(UPSTREAM)
        assert result.returncode != 0
        assert 'partial clone' in result.stderr

    def test_no_clone_mounted(self, seeded: Seeded, tmp_path: Path) -> None:
        seeded.seed = tmp_path / 'empty'
        seeded.seed.mkdir()
        result = seeded.preload(UPSTREAM)
        assert result.returncode != 0
        assert 'no git repository' in result.stderr

    def test_a_clone_borrowing_from_somewhere_unmounted(self, seeded: Seeded) -> None:
        alternates = seeded.seed / '.git' / 'objects' / 'info' / 'alternates'
        alternates.write_text('/not/mounted/objects\n', encoding='utf-8')
        result = seeded.preload(UPSTREAM)
        assert result.returncode != 0
        assert '/not/mounted/objects' in result.stderr

    def test_a_worktree_works_as_well_as_the_clone(self, seeded: Seeded, tmp_path: Path) -> None:
        worktree = tmp_path / 'worktree'
        git('worktree', 'add', '-q', str(worktree), 'v1', cwd=seeded.seed)
        seeded.seed = worktree
        result = seeded.preload(UPSTREAM)
        assert result.returncode == 0, result.stderr
        assert seeded.starting_points() == {seeded.v1, seeded.v2, seeded.v3, seeded.topic}

    def test_an_unreachable_upstream(self, seeded: Seeded) -> None:
        result = seeded.preload('/pub/scm/no/such.git')
        assert result.returncode != 0
        assert 'could not list the refs' in result.stderr

    @pytest.mark.parametrize('bad', ['demo.git', '/pub/scm/demo', '/pub/../../etc.git'])
    def test_a_path_that_is_not_a_repo_path(self, seeded: Seeded, bad: str) -> None:
        result = seeded.preload(bad)
        assert result.returncode != 0
        assert not (seeded.data / 'preload').exists()


USB = '/pub/scm/demo/usb.git'
OTHER = '/pub/scm/other/other.git'


class TestAdoptPreloads:
    @pytest.fixture
    def dirs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setattr(app, 'GROKMIRROR_PRELOAD_DIR', tmp_path / 'preload')
        monkeypatch.setattr(app, 'GROKMIRROR_OBJSTORE', tmp_path / 'objstore')
        # demo and usb share an object store, the way mainline and a
        # subsystem tree do.
        manifest = {
            UPSTREAM: {'forkgroup': 'abc123'},
            USB: {'forkgroup': 'abc123'},
            OTHER: {'forkgroup': 'def456'},
        }
        monkeypatch.setattr(app, 'MANIFEST', manifest)
        return tmp_path

    def make_preload(self, tmp_path: Path, upstream: str = UPSTREAM) -> Path:
        preload = tmp_path / 'preload' / upstream.lstrip('/')
        (preload / 'objects').mkdir(parents=True)
        (preload / 'objects' / upstream.replace('/', '_')).touch()
        return preload

    def test_a_preload_becomes_the_object_store(self, dirs: Path) -> None:
        preload = self.make_preload(dirs)
        assert app.adopt_preloads([UPSTREAM]) == [UPSTREAM]
        assert (dirs / 'objstore' / 'abc123.git' / 'objects').is_dir()
        assert not preload.exists()

    def test_a_tree_sharing_the_object_store_gets_it_too(self, dirs: Path) -> None:
        # Preload mainline, mirror only your own subsystem tree.
        self.make_preload(dirs)
        assert app.adopt_preloads([USB]) == [UPSTREAM]
        assert (dirs / 'objstore' / 'abc123.git').is_dir()

    def test_an_existing_object_store_is_kept(self, dirs: Path) -> None:
        preload = self.make_preload(dirs)
        existing = dirs / 'objstore' / 'abc123.git'
        existing.mkdir(parents=True)
        (existing / 'mine').touch()
        assert app.adopt_preloads([UPSTREAM]) == []
        assert (existing / 'mine').exists()
        # It can never be used now, and it is gigabytes.
        assert not preload.exists()

    def test_only_one_of_two_preloads_for_the_same_object_store_is_used(self, dirs: Path) -> None:
        self.make_preload(dirs)
        self.make_preload(dirs, USB)
        assert app.adopt_preloads([UPSTREAM, USB]) == [UPSTREAM]
        assert (dirs / 'objstore' / 'abc123.git' / 'objects' / UPSTREAM.replace('/', '_')).exists()
        assert app.find_preloads() == []

    def test_a_preload_nothing_picked_is_kept_for_later(self, dirs: Path) -> None:
        preload = self.make_preload(dirs)
        assert app.adopt_preloads([OTHER]) == []
        assert preload.exists()
        assert not (dirs / 'objstore' / 'abc123.git').exists()

    def test_a_preload_without_a_forkgroup_is_kept(self, dirs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app, 'MANIFEST', {UPSTREAM: {}})
        preload = self.make_preload(dirs)
        assert app.adopt_preloads([UPSTREAM]) == []
        assert preload.exists()

    def test_an_unfinished_preload_is_not_used(self, dirs: Path) -> None:
        unfinished = dirs / 'preload' / 'pub' / 'scm' / 'demo' / '.incomplete-demo.git'
        (unfinished / 'objects').mkdir(parents=True)
        assert app.find_preloads() == []
        assert app.adopt_preloads([UPSTREAM]) == []
        assert not (dirs / 'objstore').exists()

    def test_nothing_preloaded(self, dirs: Path) -> None:
        assert app.find_preloads() == []
        assert app.adopt_preloads([UPSTREAM]) == []
        assert not (dirs / 'objstore').exists()


class TestFindPreloads:
    def test_preloads_are_found_by_upstream_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app, 'GROKMIRROR_PRELOAD_DIR', tmp_path)
        (tmp_path / USB.lstrip('/') / 'objects').mkdir(parents=True)
        (tmp_path / OTHER.lstrip('/') / 'objects').mkdir(parents=True)
        assert app.find_preloads() == [USB, OTHER]

    def test_it_does_not_look_inside_a_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app, 'GROKMIRROR_PRELOAD_DIR', tmp_path)
        (tmp_path / USB.lstrip('/') / 'nested.git').mkdir(parents=True)
        assert app.find_preloads() == [USB]

    def test_no_preload_directory_at_all(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app, 'GROKMIRROR_PRELOAD_DIR', tmp_path / 'missing')
        assert app.find_preloads() == []
