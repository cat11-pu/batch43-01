import pytest

import io_uring_submit_inspector


def _inspector(**kwargs):
    return io_uring_submit_inspector.IoUringSubmitInspector(**kwargs)


def _request(opcode="READ", **overrides):
    request = {"opcode": opcode}
    request.update(overrides)
    return request


def test_opcode_outside_allowlist_fails_at_submit():
    inspector = _inspector()
    result = inspector.submit(_request("OPEN"))
    assert result["ok"] is False
    assert result["error"] == "EPERM"
    assert result["executed"] is False
    assert inspector.stats()["executed"] == 0


def test_linked_request_is_atomic_when_second_opcode_rejected():
    inspector = _inspector()
    result = inspector.submit(
        _request("READ", linked=[{"opcode": "WRITE"}, {"opcode": "OPEN"}])
    )
    assert result["ok"] is False
    assert result["error"] == "EPERM"
    assert result["executed"] == 0
    assert inspector.completions() == []


def test_registered_buffer_index_out_of_range_is_efault():
    inspector = _inspector()
    inspector.register_buffers([b"one", b"two"])
    result = inspector.submit(_request("READ", buffer_index=5))
    assert result["ok"] is False
    assert result["error"] == "EFAULT"
    assert inspector.stats()["executed"] == 0


def test_close_cancels_inflight_requests():
    inspector = _inspector()
    inspector.submit(_request("READ", in_flight=True))
    closed = inspector.close()
    assert closed["cancelled"] == 1
    assert inspector.stats()["cancelled"] == 1


def test_double_close_does_not_free_files_twice():
    inspector = _inspector()
    inspector.register_files(["/sandbox/app", "/sandbox/data"])
    inspector.close()
    first = inspector.stats()["freed_files"]
    inspector.close()
    assert inspector.stats()["freed_files"] == first


def test_registered_identity_cannot_be_swapped():
    inspector = _inspector()
    inspector.register_identity("build-task-1")
    result = inspector.submit(_request("READ", identity="build-task-2"))
    assert result["ok"] is False
    assert result["error"] == "EPERM"


def test_async_flag_cannot_escape_inspection():
    inspector = _inspector()
    result = inspector.submit(_request("READ", flags=["IOSQE_ASYNC"]))
    assert result["ok"] is False
    assert result["error"] == "EPERM"


def test_completion_queue_overflow_does_not_overwrite_entries():
    inspector = _inspector(queue_depth=2)
    assert inspector.submit(_request("READ"))["ok"] is True
    assert inspector.submit(_request("WRITE"))["ok"] is True
    overflow = inspector.submit(_request("READ"))
    assert overflow["ok"] is False
    assert overflow["error"] == "EOVERFLOW"
    assert inspector.completions() == ["READ", "WRITE"]


def test_ring_depth_mismatch_rejected():
    inspector = _inspector(queue_depth=8)
    result = inspector.submit(_request("READ", ring_depth=4))
    assert result["ok"] is False
    assert result["error"] == "EINVAL"


def test_fixed_file_outside_allowed_root_rejected():
    inspector = _inspector(allowed_root="/sandbox")
    with pytest.raises(io_uring_submit_inspector.IouringError) as exc:
        inspector.register_files(["/etc/passwd"])
    assert exc.value.code == "EPERM"


def test_fixed_file_index_out_of_range_is_efault():
    inspector = _inspector()
    inspector.register_files(["/sandbox/app"])
    result = inspector.submit(_request("READ", fixed_file_index=7))
    assert result["ok"] is False
    assert result["error"] == "EFAULT"


def test_timeout_with_unregistered_target_rejected():
    inspector = _inspector()
    result = inspector.submit(_request("TIMEOUT", target_fd=42))
    assert result["ok"] is False
    assert result["error"] == "EINVAL"


def test_separate_rings_do_not_share_fixed_tables():
    first = _inspector()
    second = _inspector()
    first.register_files(["/sandbox/first"])
    second.register_files(["/sandbox/second"])
    assert first.submit(_request("READ", fixed_file_index=0))["ok"] is True
    assert second.submit(_request("READ", fixed_file_index=0))["ok"] is True


def test_linked_successful_requests_execute_in_order():
    inspector = _inspector()
    result = inspector.submit(_request("READ", linked=[{"opcode": "WRITE"}]))
    assert result["ok"] is True
    assert result["executed"] == 2
    assert inspector.completions() == ["READ", "WRITE"]


def test_close_releases_fixed_files_once():
    inspector = _inspector()
    inspector.register_files(["/sandbox/app", "/sandbox/cache"])
    inspector.close()
    assert inspector.stats()["freed_files"] == 2
    inspector.close()
    assert inspector.stats()["freed_files"] == 2
