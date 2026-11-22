import os
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import pytest

import path_confinement_warden


def _warden():
    return path_confinement_warden.PathConfinementWarden(max_path_length=255)


def test_plain_relative_path_resolves_inside_root(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(root)
    resolved = _warden().resolve(root, "a/b.txt")
    assert os.path.normpath(resolved) == os.path.normpath(os.path.join(root, "a", "b.txt"))


def test_absolute_path_is_rejected():
    warden = _warden()
    with pytest.raises(path_confinement_warden.PathNotBeneath):
        warden.resolve("C:/sandbox/root", "C:/Windows/system.ini")


def test_parent_component_escaping_root_is_rejected():
    warden = _warden()
    with pytest.raises(path_confinement_warden.PathNotBeneath):
        warden.resolve("C:/sandbox/root", "../outside.txt")


def test_parent_component_that_stays_inside_is_allowed(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(root)
    resolved = _warden().resolve(root, "a/../b.txt")
    assert os.path.normpath(resolved) == os.path.normpath(os.path.join(root, "b.txt"))


def test_symlink_pointing_outside_root_is_rejected():
    warden = _warden()
    with pytest.raises(path_confinement_warden.PathEscapeError):
        warden.resolve("C:/sandbox/root", "link/passwd", symlinks={"link": "C:/outside"})


def test_symlink_followed_by_parent_component_is_rejected():
    warden = _warden()
    with pytest.raises(path_confinement_warden.PathEscapeError):
        warden.resolve(
            "C:/sandbox/root",
            "link/../../secret.txt",
            symlinks={"link": "C:/sandbox/root/sub"},
        )


def test_proc_self_fd_magic_link_is_rejected():
    warden = _warden()
    with pytest.raises(path_confinement_warden.PathEscapeError):
        warden.resolve("C:/sandbox/root", "proc/self/fd/9")


def test_empty_path_resolves_to_root(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(root)
    resolved = _warden().resolve(root, "")
    assert os.path.normpath(resolved) == os.path.normpath(root)


def test_overlong_path_is_rejected():
    warden = _warden()
    with pytest.raises(path_confinement_warden.PathEscapeError):
        warden.resolve("C:/sandbox/root", "a" * 300)


def test_unicode_normalization_is_stable_between_check_and_open(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(root)
    warden = _warden()
    composed = unicodedata.normalize("NFC", "caf\u00e9.txt")
    decomposed = unicodedata.normalize("NFD", "caf\u00e9.txt")
    assert warden.resolve(root, composed) == warden.resolve(root, decomposed)


def test_write_file_commits_content_without_temp_leftovers(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    warden = _warden()
    result = warden.write_file(str(root), "dir/a.txt", b"payload")
    assert result["committed"] is True
    assert (root / "dir" / "a.txt").read_bytes() == b"payload"
    leftovers = [path.name for path in root.rglob("*") if "tmp" in path.name.lower()]
    assert leftovers == []


def test_write_outside_root_creates_nothing(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    warden = _warden()
    with pytest.raises(path_confinement_warden.PathNotBeneath):
        warden.write_file(str(root), "../escape.txt", b"bad")
    assert not (tmp_path / "escape.txt").exists()


def test_failed_write_keeps_old_file_and_cleans_temp(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "old.txt").write_bytes(b"old")
    warden = _warden()
    result = warden.write_file(str(root), "old.txt", b"new", fail_after_temp=True)
    assert result["committed"] is False
    assert (root / "old.txt").read_bytes() == b"old"
    leftovers = [path.name for path in root.iterdir() if "tmp" in path.name.lower()]
    assert leftovers == []


def test_extract_escape_leaves_no_partial_files(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    warden = _warden()
    with pytest.raises(path_confinement_warden.PathNotBeneath):
        warden.extract(str(root), [("ok.txt", b"ok"), ("C:/escape.txt", b"bad")])
    assert list(root.iterdir()) == []


def test_concurrent_writes_of_distinct_files_all_complete(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    warden = _warden()

    def write(index):
        return warden.write_file(str(root), "f%d.txt" % index, b"data-%d" % index)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(4)))
    assert all(item["committed"] is True for item in results)
    assert sorted(path.name for path in root.iterdir()) == ["f0.txt", "f1.txt", "f2.txt", "f3.txt"]
