import os
from concurrent.futures import ThreadPoolExecutor

import pytest

import io_uring_submit_inspector


def _inspector(**kwargs):
    return io_uring_submit_inspector.IoUringSubmitInspector(**kwargs)


def _request(opcode="READ", **overrides):
    request = {"opcode": opcode}
    request.update(overrides)
    return request


def test_rejection_audit_uses_root_build_task_and_failing_opcode():
    inspector = _inspector()
    result = inspector.submit(
        _request(
            "READ",
            build_task="build-2048",
            linked=[{"opcode": "WRITE"}, {"opcode": "OPEN"}],
        )
    )
    assert result["error"] == "EPERM"
    event = inspector.stats()["audit"][-1]
    assert event["build_task"] == "build-2048"
    assert event["opcode"] == "OPEN"


def test_user_buffer_address_must_be_inside_registered_range():
    inspector = _inspector()
    inspector.register_buffers([{"addr": 0x1000, "len": 0x20}])
    bad = inspector.submit(
        _request("READ", buffer_index=0, user_addr=0x1018, user_len=0x10)
    )
    assert bad["error"] == "EFAULT"
    good = inspector.submit(
        _request("READ", buffer_index=0, user_addr=0x1018, user_len=0x08)
    )
    assert good["ok"] is True


def test_conflicting_buffer_aliases_cannot_hide_invalid_index():
    inspector = _inspector()
    inspector.register_buffers([b"one", b"two"])
    result = inspector.submit(_request("READ", buffer_index=0, buf_index=9))
    assert result["error"] == "EFAULT"
    assert inspector.completions() == []


def test_submit_after_close_is_rejected_without_execution():
    inspector = _inspector()
    inspector.close()
    result = inspector.submit(_request("READ", build_task="late-build"))
    assert result["error"] == "EBADF"
    assert result["executed"] is False
    assert inspector.stats()["audit"][-1]["build_task"] == "late-build"


def test_linked_batch_over_queue_capacity_is_fully_rejected():
    inspector = _inspector(queue_depth=2)
    assert inspector.submit(_request("READ"))["ok"] is True
    result = inspector.submit(
        _request("WRITE", linked=[{"opcode": "READ"}])
    )
    assert result["error"] == "EOVERFLOW"
    assert inspector.completions() == ["READ"]


def test_fixed_file_symlink_swap_is_checked_at_submit(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target"
    target.write_bytes(b"safe")
    inspector = _inspector(allowed_root=str(root))
    inspector.register_files([str(target)])
    target.unlink()
    os.symlink("/etc/passwd", target)
    result = inspector.submit(_request("READ", fixed_file_index=0))
    assert result["error"] == "EPERM"


def test_registered_credentials_cannot_change_per_request():
    inspector = _inspector()
    inspector.register_identity("build-task", token="credential-a")
    result = inspector.submit(
        _request("READ", identity="build-task", token="credential-b")
    )
    assert result["error"] == "EPERM"


def test_timeout_cannot_target_the_wrong_opcode():
    inspector = _inspector()
    result = inspector.submit(
        _request(
            "TIMEOUT",
            linked=[{"opcode": "READ"}],
            target_opcode="WRITE",
        )
    )
    assert result["error"] == "EINVAL"
    assert inspector.completions() == []


def test_concurrent_rings_release_only_their_own_fixed_files():
    def run(index):
        inspector = _inspector(max_fixed_files=8)
        inspector.register_files([f"/sandbox/task-{index}"])
        assert inspector.submit(_request("READ", fixed_file_index=0))["ok"] is True
        return inspector.close()["freed_files"], inspector.stats()["freed_files"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(12)))
    assert results == [(1, 1)] * 12
