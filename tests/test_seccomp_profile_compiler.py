import pytest

import seccomp_profile_compiler


def _profile(rules, arch="x86_64"):
    return {"arch": arch, "rules": rules}


def test_unknown_syscall_maps_to_enosys():
    compiler = seccomp_profile_compiler.SeccompProfileCompiler()
    result = compiler.unknown_syscall("frobnicate")
    assert result["errno"] == "ENOSYS"
    assert result["errno"] != "EPERM"


def test_x32_compat_rule_is_denied():
    compiler = seccomp_profile_compiler.SeccompProfileCompiler()
    with pytest.raises(seccomp_profile_compiler.SeccompError) as exc:
        compiler.compile(_profile([
            {"syscall": "open", "action": "allow", "arch": "x32"},
        ]))
    assert exc.value.code == "SECCOMP_X32_DENIED"


def test_truncated_argument_is_rejected():
    compiler = seccomp_profile_compiler.SeccompProfileCompiler()
    with pytest.raises(seccomp_profile_compiler.SeccompError) as exc:
        compiler.check_arg(0x100000001)
    assert exc.value.code == "SECCOMP_ARG_TRUNCATED"


def test_thread_sync_failure_leaves_no_filter():
    compiler = seccomp_profile_compiler.SeccompProfileCompiler()
    with pytest.raises(seccomp_profile_compiler.SeccompError) as exc:
        compiler.install(total_threads=4, fail_index=2)
    assert exc.value.code == "SECCOMP_TSYNC_FAILED"
    assert compiler.is_installed() is False


def test_install_applies_to_all_threads():
    compiler = seccomp_profile_compiler.SeccompProfileCompiler()
    result = compiler.install(total_threads=4)
    assert result["installed_on"] == 4
    assert result["consistent"] is True
    assert compiler.is_installed() is True


def test_filter_over_instruction_limit_is_rejected():
    compiler = seccomp_profile_compiler.SeccompProfileCompiler(
        max_instructions=4096)
    rules = [{"syscall": "read", "action": "allow"}] * 4097
    with pytest.raises(seccomp_profile_compiler.SeccompError) as exc:
        compiler.compile(_profile(rules))
    assert exc.value.code == "SECCOMP_FILTER_TOO_LONG"


def test_filter_at_instruction_limit_is_accepted():
    compiler = seccomp_profile_compiler.SeccompProfileCompiler(
        max_instructions=4096)
    rules = [{"syscall": "read", "action": "allow"}] * 4096
    result = compiler.compile(_profile(rules))
    assert result["instruction_count"] == 4096


def test_merge_keeps_rules_from_both_sets():
    compiler = seccomp_profile_compiler.SeccompProfileCompiler()
    merged = compiler.merge([
        [{"syscall": "open", "action": "allow"}],
        [{"syscall": "read", "action": "deny"}],
    ])
    assert merged["count"] == 2
    by_syscall = {rule["syscall"]: rule["action"] for rule in merged["rules"]}
    assert by_syscall == {"open": "allow", "read": "deny"}


def test_merge_overrides_only_same_syscall():
    compiler = seccomp_profile_compiler.SeccompProfileCompiler()
    merged = compiler.merge([
        [{"syscall": "open", "action": "allow"},
         {"syscall": "read", "action": "allow"}],
        [{"syscall": "open", "action": "deny"}],
    ])
    by_syscall = {rule["syscall"]: rule["action"] for rule in merged["rules"]}
    assert by_syscall["open"] == "deny"
    assert by_syscall["read"] == "allow"


def test_trace_action_without_tracer_is_rejected():
    compiler = seccomp_profile_compiler.SeccompProfileCompiler(
        tracer_attached=False)
    with pytest.raises(seccomp_profile_compiler.SeccompError) as exc:
        compiler.compile(_profile([{"syscall": "open", "action": "trace"}]))
    assert exc.value.code == "SECCOMP_TRACE_WITHOUT_TRACER"


def test_install_sets_no_new_privs():
    compiler = seccomp_profile_compiler.SeccompProfileCompiler()
    result = compiler.install(total_threads=2)
    assert result["no_new_privs"] is True


def test_architecture_mismatch_is_rejected():
    compiler = seccomp_profile_compiler.SeccompProfileCompiler(arch="x86_64")
    with pytest.raises(seccomp_profile_compiler.SeccompError) as exc:
        compiler.compile(_profile([], arch="aarch64"))
    assert exc.value.code == "SECCOMP_ARCH_MISMATCH"


def test_hot_reload_reaches_running_processes():
    compiler = seccomp_profile_compiler.SeccompProfileCompiler()
    result = compiler.hot_reload(
        _profile([{"syscall": "read", "action": "allow"}]),
        running_pids=[101, 102, 103],
    )
    assert result["updated_pids"] == [101, 102, 103]
    assert result["count"] == 3


def test_null_pointer_argument_is_rejected():
    compiler = seccomp_profile_compiler.SeccompProfileCompiler()
    with pytest.raises(seccomp_profile_compiler.SeccompError) as exc:
        compiler.validate_pointer_arg(0)
    assert exc.value.code == "SECCOMP_POINTER_INVALID"


def test_compile_reports_arch_and_rule_count():
    compiler = seccomp_profile_compiler.SeccompProfileCompiler(arch="x86_64")
    result = compiler.compile(_profile([
        {"syscall": "open", "action": "allow"},
        {"syscall": "read", "action": "allow"},
    ]))
    assert result["arch"] == "x86_64"
    assert result["instruction_count"] == 2
