import pytest

import cgroup_pid_controller


def test_fork_at_limit_returns_eagain():
    controller = cgroup_pid_controller.CgroupPidController()
    controller.set_limit("/z", 1)
    controller.fork("/z")
    with pytest.raises(cgroup_pid_controller.CgroupError) as exc:
        controller.fork("/z")
    assert exc.value.code == "EAGAIN"


def test_fork_below_limit_increments_count():
    controller = cgroup_pid_controller.CgroupPidController()
    controller.set_limit("/z", 3)
    result = controller.fork("/z")
    assert result["pids"] == 1
    assert controller.pids("/z") == 1


def test_limit_boundary_is_exact():
    controller = cgroup_pid_controller.CgroupPidController()
    controller.set_limit("/z", 3)
    assert controller.fork("/z")["pids"] == 1
    assert controller.fork("/z")["pids"] == 2
    assert controller.fork("/z")["pids"] == 3
    with pytest.raises(cgroup_pid_controller.CgroupError):
        controller.fork("/z")


def test_migration_is_atomic_and_totals_stay_stable():
    controller = cgroup_pid_controller.CgroupPidController()
    pid = controller.fork("/a")["pid"]
    root_before = controller.pids("/")
    result = controller.migrate(pid, "/a", "/b")
    assert result["src"] == 0
    assert result["dst"] == 1
    assert controller.pids("/") == root_before


def test_migration_into_full_cgroup_is_rejected():
    controller = cgroup_pid_controller.CgroupPidController()
    controller.set_limit("/dst", 1)
    controller.fork("/dst")
    pid = controller.fork("/src")["pid"]
    with pytest.raises(cgroup_pid_controller.CgroupError) as exc:
        controller.migrate(pid, "/src", "/dst")
    assert exc.value.code == "EAGAIN"


def test_parent_limit_aggregates_children():
    controller = cgroup_pid_controller.CgroupPidController()
    controller.set_limit("/", 2)
    controller.fork("/a")
    controller.fork("/a/b")
    assert controller.pids("/") == 2


def test_child_with_room_still_blocked_by_full_parent():
    controller = cgroup_pid_controller.CgroupPidController()
    controller.set_limit("/", 2)
    controller.set_limit("/a/b", 5)
    controller.fork("/a")
    controller.fork("/a/b")
    with pytest.raises(cgroup_pid_controller.CgroupError) as exc:
        controller.fork("/a/b")
    assert exc.value.code == "EAGAIN"


def test_thread_mode_counts_threads_in_limit():
    controller = cgroup_pid_controller.CgroupPidController()
    controller.set_limit("/t", 2)
    controller.set_thread_mode("/t", True)
    controller.fork("/t")
    controller.spawn_thread("/t")
    with pytest.raises(cgroup_pid_controller.CgroupError) as exc:
        controller.spawn_thread("/t")
    assert exc.value.code == "EAGAIN"


def test_zombie_keeps_quota_until_reaped():
    controller = cgroup_pid_controller.CgroupPidController()
    controller.set_limit("/z", 1)
    pid = controller.fork("/z")["pid"]
    controller.exit_process(pid, zombie=True)
    with pytest.raises(cgroup_pid_controller.CgroupError):
        controller.fork("/z")


def test_reaping_zombie_releases_quota():
    controller = cgroup_pid_controller.CgroupPidController()
    controller.set_limit("/z", 1)
    pid = controller.fork("/z")["pid"]
    controller.exit_process(pid, zombie=True)
    controller.reap(pid)
    assert controller.fork("/z")["pids"] == 1


def test_event_counter_ack_resets_alerts():
    controller = cgroup_pid_controller.CgroupPidController()
    controller.set_limit("/z", 1)
    controller.fork("/z")
    with pytest.raises(cgroup_pid_controller.CgroupError):
        controller.fork("/z")
    assert controller.events("/z")["max"] == 1
    assert controller.ack_events("/z") == {"max": 0}
    assert controller.events("/z")["max"] == 0


def test_rename_preserves_limit_and_processes():
    controller = cgroup_pid_controller.CgroupPidController()
    controller.set_limit("/team", 2)
    controller.fork("/team")
    controller.rename("/team", "/team2")
    assert controller.pids("/team2") == 1
    controller.fork("/team2")
    with pytest.raises(cgroup_pid_controller.CgroupError):
        controller.fork("/team2")


def test_disable_enable_preserves_state():
    controller = cgroup_pid_controller.CgroupPidController()
    controller.set_limit("/d", 1)
    controller.fork("/d")
    controller.disable("/d")
    controller.enable("/d")
    assert controller.pids("/d") == 1
    with pytest.raises(cgroup_pid_controller.CgroupError):
        controller.fork("/d")


def test_namespace_path_resolves_to_real_hierarchy():
    controller = cgroup_pid_controller.CgroupPidController()
    assert controller.resolve_path("/ns/container", "/team") == \
        "/ns/container/team"
    assert controller.resolve_path("", "/team") == "/team"


def test_restart_restores_limits_and_counts():
    controller = cgroup_pid_controller.CgroupPidController()
    controller.set_limit("/r", 1)
    controller.fork("/r")
    state = controller.dump_state()
    restarted = cgroup_pid_controller.CgroupPidController()
    restarted.load_state(state)
    assert restarted.pids("/r") == 1
    with pytest.raises(cgroup_pid_controller.CgroupError) as exc:
        restarted.fork("/r")
    assert exc.value.code == "EAGAIN"
