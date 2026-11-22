from concurrent.futures import ThreadPoolExecutor

import fanotify_verdict_dispatcher


def _dispatcher(**kwargs):
    return fanotify_verdict_dispatcher.FanotifyVerdictDispatcher(**kwargs)


def test_queue_overflow_fails_closed_for_new_access():
    dispatcher = _dispatcher()
    dispatcher.overflow()
    result = dispatcher.open_event("node-1", "/data/file")
    assert result["action"] == "deny"
    assert result["code"] == "FANOTIFY_QUEUE_OVERFLOW"


def test_overflow_counter_increments_per_event():
    dispatcher = _dispatcher()
    dispatcher.overflow()
    dispatcher.overflow()
    dispatcher.overflow(count=2)
    assert dispatcher.counters()["queue_overflow"] == 4


def test_concurrent_opens_of_same_node_scan_once():
    dispatcher = _dispatcher()

    def open_once(_):
        return dispatcher.open_event("node-2", "/data/file", pid=100, pid_start=1)

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(open_once, range(5)))
    assert all(item["action"] == "allow" for item in results)
    assert len({item["verdict"] for item in results}) == 1
    assert dispatcher.scan_count("node-2") == 1


def test_rename_invalidates_old_generation_verdict():
    dispatcher = _dispatcher()
    dispatcher.open_event("node-3", "/data/old", generation=1)
    assert dispatcher.verdict("node-3", generation=1) == "allow"
    dispatcher.rename("node-3", "/data/new", generation=2)
    assert dispatcher.verdict("node-3", generation=1) is None


def test_new_generation_after_rename_is_scanned_again():
    dispatcher = _dispatcher()
    dispatcher.open_event("node-4", "/data/old", generation=1)
    dispatcher.rename("node-4", "/data/new", generation=2)
    result = dispatcher.open_event("node-4", "/data/new", generation=2)
    assert result["action"] == "allow"
    assert result["scanned"] is True
    assert dispatcher.scan_count("node-4") == 2


def test_exec_event_after_open_does_not_rescan():
    dispatcher = _dispatcher()
    dispatcher.open_event("node-5", "/bin/tool")
    result = dispatcher.open_event("node-5", "/bin/tool", executable=True)
    assert result["scanned"] is False
    assert dispatcher.scan_count("node-5") == 1


def test_pid_reuse_does_not_inherit_old_verdict():
    dispatcher = _dispatcher()
    dispatcher.open_event("node-6", "/data/file", pid=200, pid_start=1)
    dispatcher.close_handle("node-6", pid=200, pid_start=1)
    result = dispatcher.open_event("node-6", "/data/file", pid=200, pid_start=2)
    assert result["scanned"] is True
    assert dispatcher.scan_count("node-6") == 2


def test_last_handle_close_removes_mount_mark():
    dispatcher = _dispatcher()
    dispatcher.open_event("node-7", "/mnt/data")
    assert dispatcher.mount_mark("node-7") is True
    dispatcher.close_handle("node-7")
    assert dispatcher.mount_mark("node-7") is False


def test_restart_denies_pending_requests_explicitly():
    dispatcher = _dispatcher()
    dispatcher.open_event("node-8", "/data/file", in_flight=True)
    assert dispatcher.pending() == ["node-8"]
    result = dispatcher.restart()
    assert result["denied_pending"] == 1
    assert dispatcher.pending() == []


def test_deleted_file_event_denied_without_path_read():
    dispatcher = _dispatcher()
    result = dispatcher.open_event("node-9", "/data/deleted", deleted=True)
    assert result["action"] == "deny"
    assert result["code"] == "FANOTIFY_DELETED"
    assert dispatcher.scan_count("node-9") == 0


def test_scan_timeout_returns_explicit_denial_code():
    dispatcher = _dispatcher()
    result = dispatcher.open_event("node-10", "/data/file", scan_result="timeout")
    assert result["action"] == "deny"
    assert result["code"] == "FANOTIFY_SCAN_TIMEOUT"


def test_two_file_nodes_have_independent_verdicts():
    dispatcher = _dispatcher()
    dispatcher.open_event("node-a", "/data/a")
    dispatcher.open_event("node-b", "/data/b")
    assert dispatcher.scan_count("node-a") == 1
    assert dispatcher.scan_count("node-b") == 1
    assert dispatcher.verdict("node-a") == "allow"
    assert dispatcher.verdict("node-b") == "allow"


def test_full_pending_queue_triggers_fail_closed():
    dispatcher = _dispatcher(queue_capacity=1)
    assert dispatcher.open_event("node-11", "/data/one", in_flight=True)["action"] == "pending"
    overflow = dispatcher.open_event("node-12", "/data/two", in_flight=True)
    assert overflow["action"] == "deny"
    assert overflow["code"] == "FANOTIFY_QUEUE_OVERFLOW"


def test_restart_clears_cached_verdict_and_rescans():
    dispatcher = _dispatcher()
    dispatcher.open_event("node-13", "/data/file")
    dispatcher.restart()
    result = dispatcher.open_event("node-13", "/data/file")
    assert result["scanned"] is True
    assert dispatcher.scan_count("node-13") == 2


def test_denied_counter_tracks_denials():
    dispatcher = _dispatcher()
    dispatcher.overflow()
    dispatcher.open_event("node-14", "/data/one")
    dispatcher.open_event("node-15", "/data/two", deleted=True)
    assert dispatcher.counters()["denied"] == 2
