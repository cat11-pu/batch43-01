"""Seccomp profile compiler (skeleton)."""


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
        self.arch = arch
        self.max_instructions = max_instructions
        self.tracer_attached = tracer_attached
        self.no_new_privs = no_new_privs
        self._installed = False

    def add_rule(self, syscall, action="allow", arch=None):
        """Append one rule; returns {"rules": int}."""
        raise NotImplementedError

    def compile(self, profile):
        """Compile a profile of rules.

        ``profile`` is {"arch": str, "rules": list}.  Errors:
        SECCOMP_ARCH_MISMATCH, SECCOMP_X32_DENIED (any 32-bit compat rule),
        SECCOMP_TRACE_WITHOUT_TRACER and SECCOMP_FILTER_TOO_LONG when the rule
        count exceeds ``max_instructions`` (never silently truncated).
        Returns {"arch": str, "instruction_count": int, "filters": int}.
        """
        raise NotImplementedError

    def merge(self, rule_sets):
        """Merge rule sets per syscall.

        Later rules override the same syscall only; earlier unrelated rules
        stay.  Returns {"rules": list, "count": int}.
        """
        raise NotImplementedError

    def unknown_syscall(self, name):
        """Resolve an unknown syscall to ENOSYS, never EPERM.

        Returns {"errno": "ENOSYS"}.
        """
        raise NotImplementedError

    def check_arg(self, value):
        """Reject arguments whose high bits would be truncated.

        Values above 0xFFFFFFFF raise SeccompError("SECCOMP_ARG_TRUNCATED").
        """
        raise NotImplementedError

    def install(self, total_threads=1, fail_index=None):
        """Install the filter on every thread atomically.

        A thread sync failure raises SeccompError("SECCOMP_TSYNC_FAILED") and
        must leave no half-installed filter.  Returns {"installed_on": int,
        "consistent": True, "no_new_privs": bool}.
        """
        raise NotImplementedError

    def is_installed(self):
        """Return True only after a fully consistent install."""
        raise NotImplementedError

    def hot_reload(self, profile, running_pids):
        """Reload the profile for every running process, not only new ones.

        Returns {"updated_pids": list, "count": int}.
        """
        raise NotImplementedError

    def validate_pointer_arg(self, pointer):
        """Reject null or out-of-range syscall pointer arguments.

        Raises SeccompError("SECCOMP_POINTER_INVALID").
        """
        raise NotImplementedError
