from concurrent.futures import ThreadPoolExecutor

import bpf_program_admission


HELPERS = {
    "sock_filter": {"skb_load_bytes"},
    "sched_cls": {"skb_load_bytes", "bpf_redirect"},
}


def _admission(**kwargs):
    return bpf_program_admission.BpfProgramAdmission(allowed_helpers=HELPERS, **kwargs)


def _program(program_id="p1", **overrides):
    program = {
        "id": program_id,
        "type": "sched_cls",
        "tenant": "tenant-a",
        "helpers": ["skb_load_bytes"],
        "pin_path": "/sys/fs/bpf/tenant-a/map%d" % program_id,
        "sleepable": False,
        "tail_calls": [],
        "license": "GPL",
        "privileged": True,
        "map_capacity": 64,
        "attach_cgroup": "/sys/fs/cgroup/tenant-a/app",
        "attach_ok": True,
        "maps_used": [],
    }
    program.update(overrides)
    return program


def test_helper_outside_program_type_allowlist_rejected():
    admission = _admission()
    result = admission.load(_program(helpers=["bpf_get_current_pid_tgid"]))
    assert result["ok"] is False
    assert result["error"] == "EPERM"
    assert result["code"] == "BPF_HELPER_DENIED"
    assert "BPF_HELPER_DENIED" in result["validator_log"]


def test_socket_filter_helper_not_shared_with_network_program():
    admission = _admission()
    result = admission.load(_program(type="sock_filter", helpers=["bpf_redirect"]))
    assert result["ok"] is False
    assert result["code"] == "BPF_HELPER_DENIED"


def test_map_pin_path_crossing_tenant_namespace_rejected():
    admission = _admission(tenant="tenant-a")
    result = admission.load(_program(pin_path="/sys/fs/bpf/tenant-b/map1"))
    assert result["ok"] is False
    assert result["code"] == "BPF_MAP_TENANT_MISMATCH"


def test_tail_call_to_other_tenant_rejected():
    admission = _admission()
    result = admission.load(
        _program(tail_calls=[{"program_id": "p9", "tenant": "tenant-b"}])
    )
    assert result["ok"] is False
    assert result["code"] == "BPF_TAILCALL_TENANT"


def test_concurrent_load_same_pin_path_only_one_succeeds():
    admission = _admission()
    first = _program("p1", pin_path="/sys/fs/bpf/tenant-a/shared")
    second = _program("p2", pin_path="/sys/fs/bpf/tenant-a/shared")

    def load(program):
        return admission.load(program)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(load, [first, second]))
    assert sum(1 for item in results if item["ok"]) == 1
    assert any(item.get("error") == "EEXIST" for item in results)


def test_sleepable_program_rejected_on_normal_load_path():
    admission = _admission()
    result = admission.load(_program(sleepable=True))
    assert result["ok"] is False
    assert result["code"] == "BPF_SLEEPABLE_DENIED"


def test_cgroup_attachment_outside_tenant_root_rejected():
    admission = _admission(cgroup_root="/sys/fs/cgroup/tenant-a")
    result = admission.load(_program(attach_cgroup="/sys/fs/cgroup/tenant-b/app"))
    assert result["ok"] is False
    assert result["code"] == "BPF_CGROUP_ESCAPE"


def test_empty_license_rejected():
    admission = _admission()
    result = admission.load(_program(license=""))
    assert result["ok"] is False
    assert result["code"] == "BPF_LICENSE_DENIED"


def test_unprivileged_load_rejected():
    admission = _admission()
    result = admission.load(_program(privileged=False))
    assert result["ok"] is False
    assert result["code"] == "BPF_UNPRIVILEGED"


def test_map_capacity_mismatch_rejected():
    admission = _admission(map_capacity=64)
    result = admission.load(_program(map_capacity=128))
    assert result["ok"] is False
    assert result["code"] == "EINVAL"


def test_unload_busy_when_map_still_referenced():
    admission = _admission()
    shared = "/sys/fs/bpf/tenant-a/shared-map"
    first = admission.load(_program("a", pin_path="/sys/fs/bpf/tenant-a/a", maps_used=[shared]))
    second = admission.load(_program("b", pin_path="/sys/fs/bpf/tenant-a/b", maps_used=[shared]))
    assert first["ok"] is True
    assert second["ok"] is True
    busy = admission.unload("a")
    assert busy["ok"] is False
    assert busy["error"] == "EBUSY"


def test_attach_failure_cleans_up_pin_path():
    admission = _admission()
    result = admission.load(_program(attach_ok=False))
    assert result["ok"] is False
    assert result["cleanup"] is True
    assert admission.pin_path("p1") is None


def test_double_unload_is_idempotent():
    admission = _admission()
    admission.load(_program())
    first = admission.unload("p1")
    second = admission.unload("p1")
    assert first["ok"] is True
    assert second["ok"] is True


def test_valid_program_loads_pins_and_unloads():
    admission = _admission()
    loaded = admission.load(_program())
    assert loaded["ok"] is True
    assert admission.pin_path("p1") == "/sys/fs/bpf/tenant-a/mapp1"
    assert "p1" in admission.loaded()
    unloaded = admission.unload("p1")
    assert unloaded["ok"] is True
    assert "p1" not in admission.loaded()


def test_tail_call_within_same_tenant_allowed():
    admission = _admission()
    result = admission.load(_program(tail_calls=[{"program_id": "p2", "tenant": "tenant-a"}]))
    assert result["ok"] is True
