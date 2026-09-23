"""Tests for powerwash.sh, which empties the data volume but for git history.

The script is run for real against a directory standing in for /data. The
container's lock is taken the way the entrypoint takes it, with flock(1)
on the same file, from a process that stays alive for the test.
"""

import os
import pty
import subprocess
from pathlib import Path
from typing import Iterator, Optional

import pytest

SCRIPT = Path(__file__).parent.parent / 'powerwash.sh'


OBJSTORE = 'grokmirror/objstore'
PRELOAD = 'grokmirror/preload'
HISTORY = [
    f'{OBJSTORE}/abc123.git/objects/pack/pack-1.pack',
    f'{PRELOAD}/pub/scm/linux/kernel/git/torvalds/linux.git/objects/pack/pack-2.pack',
]
EVERYTHING_ELSE = [
    'venv/bin/python',
    'identity.json',
    '.hidden',
    'xdg/config/korgalore/korgalore.toml',
    'grokmirror/grokmirror.conf',
    'grokmirror/manifest.js.gz',
    'grokmirror/repos/pub/scm/linux/kernel/git/gregkh/usb.git/objects/info/alternates',
    'grokmirror/preload-notes.txt',
]


@pytest.fixture
def data(tmp_path: Path) -> Path:
    """A volume that looks lived in."""
    data = tmp_path / 'data'
    for rel in HISTORY + EVERYTHING_ELSE:
        (data / rel).parent.mkdir(parents=True, exist_ok=True)
        (data / rel).write_text(f'{rel}\n', encoding='utf-8')
    return data


def files(data: Path) -> set[str]:
    """Every file left in the volume, relative to it, but for the lock."""
    return {p.relative_to(data).as_posix() for p in data.rglob('*') if p.is_file() and p.name != '.lock'}


def powerwash(
    data: Path,
    *args: str,
    stdin: Optional[int] = subprocess.DEVNULL,
    objstore: str = OBJSTORE,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        'DATA_DIR': str(data),
        'GROKMIRROR_OBJSTORE': str(data / objstore),
        'GROKMIRROR_PRELOAD_DIR': str(data / PRELOAD),
    }
    return subprocess.run(
        [str(SCRIPT), *args],
        env=env,
        stdin=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def on_a_tty(data: Path, answer: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run it with a terminal on stdin, and type `answer' at the prompt."""
    main, other = pty.openpty()
    try:
        os.write(main, f'{answer}\n'.encode())
        return powerwash(data, *args, stdin=other)
    finally:
        os.close(main)
        os.close(other)


def contents(data: Path) -> set[str]:
    return {path.name for path in data.iterdir()}


def untouched(data: Path, before: set[str]) -> bool:
    """Whether a refusal left the volume alone. The lock file doesn't
    count: taking the lock creates it, and every start does that too."""
    return contents(data) - {'.lock'} == before - {'.lock'}


@pytest.fixture
def running(data: Path) -> Iterator[None]:
    """The container, as far as the lock can tell: a process holding it."""
    holder = subprocess.Popen(
        ['flock', str(data / '.lock'), 'sh', '-c', 'echo locked; exec sleep 60'],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline() == 'locked\n'
    try:
        yield
    finally:
        holder.kill()
        holder.wait()


class TestPowerwash:
    def test_it_keeps_git_history_and_nothing_else(self, data: Path) -> None:
        result = powerwash(data, '--yes')
        assert result.returncode == 0, result.stderr
        assert files(data) == set(HISTORY)
        assert 'git history is kept' in result.stderr

    def test_the_lock_file_is_left_for_the_next_start(self, data: Path) -> None:
        powerwash(data, '--yes')
        assert (data / '.lock').exists()

    def test_remove_git_removes_the_history_too(self, data: Path) -> None:
        result = powerwash(data, '--yes', '--remove-git')
        assert result.returncode == 0, result.stderr
        assert contents(data) == {'.lock'}
        assert 'the volume is empty' in result.stderr

    def test_it_follows_where_the_objstore_is_configured(self, data: Path) -> None:
        moved = 'elsewhere/objstore'
        (data / 'elsewhere').mkdir()
        (data / OBJSTORE).rename(data / moved)
        assert powerwash(data, '--yes', objstore=moved).returncode == 0
        assert files(data) == {HISTORY[1], f'{moved}/abc123.git/objects/pack/pack-1.pack'}

    def test_an_empty_objstore_is_no_history(self, data: Path) -> None:
        powerwash(data, '--yes', '--remove-git')
        (data / OBJSTORE).mkdir(parents=True)
        (data / 'identity.json').write_text('{}\n', encoding='utf-8')
        result = powerwash(data, '--yes')
        assert result.returncode == 0, result.stderr
        assert 'the volume is empty' in result.stderr

    def test_history_alone_is_nothing_to_do(self, data: Path) -> None:
        powerwash(data, '--yes')
        result = powerwash(data, '--yes')
        assert result.returncode == 0
        assert 'nothing to remove but git history' in result.stderr
        assert '--remove-git' in result.stderr
        assert files(data) == set(HISTORY)

    def test_it_refuses_while_the_container_runs(self, data: Path, running: None) -> None:
        before = contents(data)
        result = powerwash(data, '--yes')
        assert result.returncode == 1
        assert 'still running' in result.stderr
        assert untouched(data, before)

    def test_it_works_once_the_container_has_stopped(self, data: Path) -> None:
        # The lock belongs to a process, not to the file: a container that
        # has exited leaves the file behind, and that must not block this.
        (data / '.lock').touch()
        assert powerwash(data, '--yes').returncode == 0
        assert files(data) == set(HISTORY)

    def test_without_a_terminal_it_asks_for_yes(self, data: Path) -> None:
        before = contents(data)
        result = powerwash(data)
        assert result.returncode == 1
        assert '-it' in result.stderr
        assert untouched(data, before)

    def test_typing_powerwash_goes_ahead(self, data: Path) -> None:
        result = on_a_tty(data, 'powerwash')
        assert result.returncode == 0, result.stderr
        assert 'Type "powerwash"' in result.stderr
        assert 'git history they share is kept' in result.stderr
        assert files(data) == set(HISTORY)

    def test_the_question_says_when_history_goes_too(self, data: Path) -> None:
        result = on_a_tty(data, 'powerwash', '--remove-git')
        assert result.returncode == 0, result.stderr
        assert 'This removes everything' in result.stderr
        assert 'kept' not in result.stderr
        assert contents(data) == {'.lock'}

    @pytest.mark.parametrize('answer', ['', 'y', 'yes', 'Powerwash'])
    def test_any_other_answer_changes_nothing(self, data: Path, answer: str) -> None:
        before = contents(data)
        result = on_a_tty(data, answer)
        assert result.returncode == 1
        assert 'nothing was removed' in result.stderr
        assert untouched(data, before)

    def test_an_empty_volume_is_a_hint_about_v(self, tmp_path: Path) -> None:
        # What a forgotten -v looks like: the image's VOLUME /data gives the
        # container a new empty volume.
        empty = tmp_path / 'empty'
        empty.mkdir()
        result = powerwash(empty, '--yes')
        assert result.returncode == 0
        assert 'already empty' in result.stderr
        assert '-v' in result.stderr

    def test_a_missing_data_dir_is_an_error(self, tmp_path: Path) -> None:
        result = powerwash(tmp_path / 'nope', '--yes')
        assert result.returncode == 1
        assert 'mount the volume' in result.stderr

    def test_unknown_arguments_are_refused(self, data: Path) -> None:
        before = contents(data)
        result = powerwash(data, '--all')
        assert result.returncode == 1
        assert "unknown argument '--all'" in result.stderr
        assert '--remove-git' in result.stderr
        assert untouched(data, before)

    def test_it_does_not_follow_symlinks_out_of_the_volume(self, data: Path, tmp_path: Path) -> None:
        outside = tmp_path / 'outside'
        outside.mkdir()
        (outside / 'keep').write_text('x\n', encoding='utf-8')
        (data / 'link').symlink_to(outside)
        assert powerwash(data, '--yes').returncode == 0
        assert (outside / 'keep').exists()

    def test_a_symlink_on_the_way_to_the_objstore_is_not_followed(self, data: Path, tmp_path: Path) -> None:
        # /data/grokmirror pointing out of the volume: the history "in" it
        # is not ours to keep or to remove, and the link just goes.
        outside = tmp_path / 'outside'
        (data / 'grokmirror').rename(outside)
        (data / 'grokmirror').symlink_to(outside)
        assert powerwash(data, '--yes').returncode == 0
        assert not (data / 'grokmirror').is_symlink()
        assert (outside / 'grokmirror.conf').exists()
