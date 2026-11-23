"""Pre-launch stress tests: limits must hold under concurrency."""

import threading

from cgroup_pid_controller import CgroupError, CgroupPidController


def _hammer_fork(controller, cgroup, attempts, results):
    for _ in range(attempts):
        try:
            controller.fork(cgroup)
            results.append("ok")
        except CgroupError as exc:
            assert exc.code == "EAGAIN"
            results.append("full")


def test_concurrent_forks_never_exceed_limit():
    controller = CgroupPidController()
    controller.set_limit("/tenant", 200)
    results = []
    threads = [
        threading.Thread(target=_hammer_fork,
                         args=(controller, "/tenant", 50, results))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("ok") == 200
    assert controller.pids("/tenant") == 200
    assert controller.events("/tenant")["max"] == results.count("full")


def test_concurrent_migrations_never_breach_dst_limit():
    controller = CgroupPidController()
    controller.set_limit("/dst", 100)
    pids = [controller.fork("/src")["pid"] for _ in range(400)]
    errors = []

    def mover(chunk):
        for pid in chunk:
            try:
                controller.migrate(pid, "/src", "/dst")
            except CgroupError as exc:
                errors.append(exc.code)

    chunks = [pids[i::4] for i in range(4)]
    workers = [threading.Thread(target=mover, args=(c,)) for c in chunks]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert controller.pids("/dst") == 100
    assert controller.pids("/src") == 300
    assert controller.pids("/") == 400
    assert set(errors) == {"EAGAIN"}


def test_zombie_churn_holds_quota_until_reaped():
    controller = CgroupPidController()
    controller.set_limit("/svc", 64)
    live = [controller.fork("/svc")["pid"] for _ in range(64)]
    for pid in live[:32]:
        controller.exit_process(pid, zombie=True)
    for _ in range(32):
        try:
            controller.fork("/svc")
            raise AssertionError("zombies must hold quota")
        except CgroupError as exc:
            assert exc.code == "EAGAIN"
    for pid in live[:32]:
        controller.reap(pid)
    for _ in range(32):
        controller.fork("/svc")
    assert controller.pids("/svc") == 64


def test_restart_under_load_restores_exact_limits():
    controller = CgroupPidController()
    for index in range(50):
        controller.set_limit("/t%d" % index, index + 1)
        for _ in range(index + 1):
            controller.fork("/t%d" % index)
    state = controller.dump_state()
    restarted = CgroupPidController()
    restarted.load_state(state)
    assert restarted.pids("/") == controller.pids("/")
    for index in range(50):
        try:
            restarted.fork("/t%d" % index)
            raise AssertionError("limit lost after restart")
        except CgroupError as exc:
            assert exc.code == "EAGAIN"
