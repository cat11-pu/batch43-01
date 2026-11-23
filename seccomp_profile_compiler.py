"""Seccomp profile compiler."""

import threading


_ARCH_ALIASES = {
    "amd64": "x86_64",
    "x64": "x86_64",
    "arm64": "aarch64",
    "i386": "x86",
    "i686": "x86",
}

_X32_ARCHES = {"x32", "x86_32", "x86_64_x32"}

_MAX_USER_POINTERS = {
    "x86_64": 0x00007FFFFFFFFFFF,
    "x86": 0xFFFFFFFF,
    "aarch64": 0x0000FFFFFFFFFFFF,
}


def _normalise_arch(arch):
    if arch is None:
        return None
    value = str(arch).strip().lower()
    return _ARCH_ALIASES.get(value, value)


class SeccompError(Exception):
    """Error carrying a stable seccomp error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class SeccompProfileCompiler:
    """Compiles syscall rules into one BPF filter program."""

    def __init__(self, arch="x86_64", max_instructions=4096,
                 tracer_attached=False, no_new_privs=True):
        self.arch = _normalise_arch(arch) or "x86_64"
        self.max_instructions = max_instructions
        self.tracer_attached = tracer_attached
        self.no_new_privs = no_new_privs
        self._installed = False
        self._install_consistent = False
        self._installed_threads = ()
        self._rules = []
        self._active_profile = None
        self._reload_generation = 0
        self._install_lock = threading.Lock()

    @staticmethod
    def _raise(code, detail=""):
        raise SeccompError(code, detail)

    @staticmethod
    def _rule_is_x32(rule):
        for key in ("x32", "is_x32", "compat", "is_compat", "compat32"):
            if bool(rule.get(key)):
                return True

        rule_arch = _normalise_arch(rule.get("arch"))
        if rule_arch in _X32_ARCHES:
            return True

        syscall = rule.get("syscall")
        if isinstance(syscall, int) and not isinstance(syscall, bool):
            return bool(syscall & 0x40000000)
        if isinstance(syscall, str):
            return syscall.lower().startswith(("__x32_", "x32_"))
        return False

    def _validate_rule(self, rule, inherited_arch):
        if not isinstance(rule, dict):
            self._raise("SECCOMP_INVALID_RULE", "rule must be a mapping")
        if "syscall" not in rule:
            self._raise("SECCOMP_INVALID_RULE", "rule has no syscall")

        if self._rule_is_x32(rule):
            self._raise("SECCOMP_X32_DENIED", "32-bit compatibility syscall")

        rule_arch = _normalise_arch(rule.get("arch", inherited_arch))
        if rule_arch is not None and rule_arch != self.arch:
            self._raise(
                "SECCOMP_ARCH_MISMATCH",
                "rule architecture %s does not match %s"
                % (rule_arch, self.arch),
            )

        action = rule.get("action", "allow")
        action_name = str(action).strip().lower()
        if "trace" in action_name and not self.tracer_attached:
            self._raise(
                "SECCOMP_TRACE_WITHOUT_TRACER",
                "trace action requires an attached tracer",
            )

    def add_rule(self, syscall, action="allow", arch=None):
        """Append one rule; returns {"rules": int}."""
        rule = {"syscall": syscall, "action": action}
        if arch is not None:
            rule["arch"] = arch

        self._validate_rule(rule, self.arch)
        if len(self._rules) + 1 > self.max_instructions:
            self._raise(
                "SECCOMP_FILTER_TOO_LONG",
                "instruction limit is %s" % self.max_instructions,
            )

        self._rules.append(rule)
        return {"rules": len(self._rules)}

    def compile(self, profile=None):
        """Compile a profile of rules.

        ``profile`` is {"arch": str, "rules": list}.  Errors:
        SECCOMP_ARCH_MISMATCH, SECCOMP_X32_DENIED (any 32-bit compat rule),
        SECCOMP_TRACE_WITHOUT_TRACER and SECCOMP_FILTER_TOO_LONG when the rule
        count exceeds ``max_instructions`` (never silently truncated).
        Returns {"arch": str, "instruction_count": int, "filters": int}.
        """
        if profile is None:
            profile = {"arch": self.arch, "rules": list(self._rules)}
        elif isinstance(profile, (list, tuple)):
            profile = {"arch": self.arch, "rules": list(profile)}
        elif not isinstance(profile, dict):
            self._raise("SECCOMP_INVALID_PROFILE", "profile must be a mapping")

        if self._rule_is_x32(profile):
            self._raise("SECCOMP_X32_DENIED", "32-bit compatibility profile")

        profile_arch = _normalise_arch(profile.get("arch", self.arch))
        if profile_arch in _X32_ARCHES:
            self._raise("SECCOMP_X32_DENIED", "32-bit compatibility profile")
        if profile_arch is not None and profile_arch != self.arch:
            self._raise(
                "SECCOMP_ARCH_MISMATCH",
                "profile architecture %s does not match %s"
                % (profile_arch, self.arch),
            )

        rules = profile.get("rules", [])
        if not isinstance(rules, (list, tuple)):
            self._raise("SECCOMP_INVALID_PROFILE", "rules must be a sequence")

        compiled_rules = []
        for rule in rules:
            self._validate_rule(rule, profile_arch or self.arch)
            compiled_rules.append(dict(rule))

        if len(compiled_rules) > self.max_instructions:
            self._raise(
                "SECCOMP_FILTER_TOO_LONG",
                "%s instructions exceed limit %s"
                % (len(compiled_rules), self.max_instructions),
            )

        self._active_profile = {
            "arch": self.arch,
            "rules": compiled_rules,
        }
        return {
            "arch": self.arch,
            "instruction_count": len(compiled_rules),
            "filters": 1,
        }

    def merge(self, rule_sets):
        """Merge rule sets per syscall.

        Later rules override the same syscall only; earlier unrelated rules
        stay.  Returns {"rules": list, "count": int}.
        """
        merged = {}
        for rule_set in rule_sets:
            if rule_set is None:
                continue
            if isinstance(rule_set, dict):
                rule_set = [rule_set]
            for rule in rule_set:
                if not isinstance(rule, dict) or "syscall" not in rule:
                    self._raise(
                        "SECCOMP_INVALID_RULE",
                        "merged rule must contain a syscall",
                    )
                merged[rule["syscall"]] = dict(rule)

        rules = list(merged.values())
        return {"rules": rules, "count": len(rules)}

    def unknown_syscall(self, name):
        """Resolve an unknown syscall to ENOSYS, never EPERM.

        Returns {"errno": "ENOSYS"}.
        """
        return {"errno": "ENOSYS"}

    def check_arg(self, value):
        """Reject arguments whose high bits would be truncated.

        Values above 0xFFFFFFFF raise SeccompError("SECCOMP_ARG_TRUNCATED").
        """
        if (not isinstance(value, int) or isinstance(value, bool)
                or value < 0 or value > 0xFFFFFFFF):
            self._raise(
                "SECCOMP_ARG_TRUNCATED",
                "argument does not fit in 32 bits",
            )

    def install(self, total_threads=1, fail_index=None):
        """Install the filter on every thread atomically.

        A thread sync failure raises SeccompError("SECCOMP_TSYNC_FAILED") and
        must leave no half-installed filter.  Returns {"installed_on": int,
        "consistent": True, "no_new_privs": bool}.
        """
        if not isinstance(total_threads, int) or isinstance(total_threads, bool):
            raise ValueError("total_threads must be an integer")
        if total_threads < 0:
            raise ValueError("total_threads must not be negative")

        with self._install_lock:
            if (isinstance(fail_index, int)
                    and not isinstance(fail_index, bool)
                    and 0 <= fail_index < total_threads):
                self._installed = False
                self._install_consistent = False
                self._installed_threads = ()
                self._raise(
                    "SECCOMP_TSYNC_FAILED",
                    "thread %s failed to synchronise" % fail_index,
                )

            installed_threads = tuple(range(total_threads))
            self.no_new_privs = True
            self._installed_threads = installed_threads
            self._install_consistent = True
            self._installed = True

            return {
                "installed_on": len(installed_threads),
                "consistent": True,
                "no_new_privs": True,
            }

    def is_installed(self):
        """Return True only after a fully consistent install."""
        return bool(self._installed and self._install_consistent)

    def hot_reload(self, profile, running_pids):
        """Reload the profile for every running process, not only new ones.

        Returns {"updated_pids": list, "count": int}.
        """
        if running_pids is None:
            pids = []
        elif isinstance(running_pids, int):
            pids = [running_pids]
        else:
            pids = list(running_pids)

        updated_pids = []
        seen = set()
        for pid in pids:
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                raise ValueError("running_pids must contain positive integers")
            if pid not in seen:
                seen.add(pid)
                updated_pids.append(pid)

        self.compile(profile)
        self._reload_generation += 1
        return {"updated_pids": updated_pids, "count": len(updated_pids)}

    def validate_pointer_arg(self, pointer):
        """Reject null or out-of-range syscall pointer arguments.

        Raises SeccompError("SECCOMP_POINTER_INVALID").
        """
        max_pointer = _MAX_USER_POINTERS.get(self.arch, 0x00007FFFFFFFFFFF)
        if (not isinstance(pointer, int) or isinstance(pointer, bool)
                or pointer <= 0 or pointer > max_pointer):
            self._raise(
                "SECCOMP_POINTER_INVALID",
                "pointer is null or outside userspace",
            )
