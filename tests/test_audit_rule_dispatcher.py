import pytest

import audit_rule_dispatcher


def test_security_event_on_full_queue_fails_closed():
    dispatcher = audit_rule_dispatcher.AuditRuleDispatcher(queue_size=1)
    dispatcher.enqueue({"type": "syscall", "security": False})
    with pytest.raises(audit_rule_dispatcher.AuditError) as exc:
        dispatcher.enqueue({"type": "auth", "security": True})
    assert exc.value.code == "AUDIT_BACKLOG_OVERFLOW"


def test_non_security_overflow_is_dropped_with_code():
    dispatcher = audit_rule_dispatcher.AuditRuleDispatcher(queue_size=1)
    dispatcher.enqueue({"type": "syscall", "security": False})
    result = dispatcher.enqueue({"type": "syscall", "security": False})
    assert result["dropped"] is True
    assert result["code"] == "AUDIT_BACKLOG_OVERFLOW"


def test_auth_failures_are_not_rate_limited():
    dispatcher = audit_rule_dispatcher.AuditRuleDispatcher(queue_size=50)
    for index in range(10):
        result = dispatcher.enqueue({"type": "auth", "security": True,
                                     "message": "login failed %d" % index})
        assert result["code"] is None
    assert len(dispatcher.log_entries()) == 10


def test_never_match_rule_does_not_hide_always_record():
    dispatcher = audit_rule_dispatcher.AuditRuleDispatcher()
    dispatcher.add_rule({"id": "never", "match": "never", "record": False})
    dispatcher.add_rule({"id": "always", "match": "always", "record": True})
    result = dispatcher.dispatch({"type": "auth"})
    assert result["matched"] == ["always"]
    assert result["recorded"] is True


def test_directory_rename_updates_watch_path():
    dispatcher = audit_rule_dispatcher.AuditRuleDispatcher()
    dispatcher.rename_watch("/var/log/app", "/var/log/app2")
    paths = dispatcher.watch_paths()
    assert "/var/log/app2" in paths
    assert "/var/log/app" not in paths


def test_exclusion_does_not_match_sibling_prefix():
    dispatcher = audit_rule_dispatcher.AuditRuleDispatcher()
    dispatcher.add_exclusion("/home/user/secret")
    assert dispatcher.is_excluded("/home/user/secret2") is False
    assert dispatcher.is_excluded("/home/user/secret/file") is True


def test_reload_replaces_rules_atomically():
    dispatcher = audit_rule_dispatcher.AuditRuleDispatcher()
    dispatcher.add_rule({"id": "old", "match": "always", "record": True})
    result = dispatcher.reload([
        {"id": "new", "match": "always", "record": True},
    ])
    assert result["rules"] == 1
    dispatched = dispatcher.dispatch({"type": "auth"})
    assert dispatched["matched"] == ["new"]
    assert len(dispatcher.log_entries()) == 1


def test_duplicate_rule_insert_is_idempotent():
    dispatcher = audit_rule_dispatcher.AuditRuleDispatcher()
    rule = {"id": "r1", "match": "always", "record": True}
    dispatcher.add_rule(rule)
    result = dispatcher.add_rule(dict(rule))
    assert result["rules"] == 1


def test_multipart_event_is_assembled_once():
    dispatcher = audit_rule_dispatcher.AuditRuleDispatcher()
    first = dispatcher.record_multipart("e1", "first", 2)
    second = dispatcher.record_multipart("e1", "second", 2)
    assert first["complete"] is False
    assert second["complete"] is True
    entry = dispatcher.log_entries()[-1]
    assert entry["parts"] == ["first", "second"]


def test_missing_multipart_part_is_marked_incomplete():
    dispatcher = audit_rule_dispatcher.AuditRuleDispatcher()
    dispatcher.record_multipart("e2", "first", 2)
    assert "e2" in dispatcher.multipart_incomplete()


def test_cron_event_gets_unset_auid():
    dispatcher = audit_rule_dispatcher.AuditRuleDispatcher()
    normalized = dispatcher.normalize_event({"type": "syscall",
                                             "cron": True})
    assert normalized["auid"] == "unset"


def test_compat_arch_event_is_classified_b32():
    dispatcher = audit_rule_dispatcher.AuditRuleDispatcher()
    assert dispatcher.classify_arch("i386")["arch"] == "b32"
    assert dispatcher.classify_arch("x86_64")["arch"] == "b64"


def test_disk_full_stops_writes_but_keeps_daemon_running():
    dispatcher = audit_rule_dispatcher.AuditRuleDispatcher()
    dispatcher.mark_disk_full()
    with pytest.raises(audit_rule_dispatcher.AuditError) as exc:
        dispatcher.enqueue({"type": "auth", "security": True})
    assert exc.value.code == "AUDIT_DISK_FULL"
    assert dispatcher.is_running() is True


def test_container_namespace_event_reaches_main_log():
    dispatcher = audit_rule_dispatcher.AuditRuleDispatcher()
    dispatcher.enqueue({"type": "syscall", "security": False,
                        "namespace": "container", "message": "exec"})
    assert dispatcher.log_entries()[-1]["namespace"] == "container"


def test_rotation_flushes_pending_events():
    dispatcher = audit_rule_dispatcher.AuditRuleDispatcher()
    dispatcher.enqueue({"type": "syscall", "security": False})
    dispatcher.enqueue({"type": "syscall", "security": False})
    result = dispatcher.rotate()
    assert result == {"flushed": True, "lost": 0}
