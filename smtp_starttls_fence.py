"""SMTP STARTTLS policy fence and delivery gate."""

import fnmatch
import re
import threading

_CAPABILITY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")


class SmtpError(Exception):
    """Configuration failure raised by the fence."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class _Policy:
    """Immutable MTA-STS style policy record."""

    __slots__ = ("domain", "mode", "mx_patterns", "max_age", "created_at")

    def __init__(self, domain, mode, mx_patterns, max_age, created_at):
        self.domain = domain.lower()
        self.mode = mode
        self.mx_patterns = tuple(str(p).lower() for p in mx_patterns)
        self.max_age = max_age
        self.created_at = created_at

    def is_live(self, now):
        return (now - self.created_at) <= self.max_age

    def mx_matches(self, host):
        host = host.lower()
        return any(fnmatch.fnmatchcase(host, pattern) for pattern in self.mx_patterns)


class SmtpStarttlsFence:
    """Evaluates MTA-STS style policies before any plaintext delivery attempt."""

    def __init__(self):
        self._lock = threading.Lock()
        self._policies = {}
        self._reports = []

    def set_policy(self, domain, mode="enforce", mx_patterns=("*",), max_age=3600, created_at=0.0):
        policy = _Policy(domain, mode, mx_patterns, max_age, created_at)
        with self._lock:
            policies = dict(self._policies)
            policies[policy.domain] = policy
            self._policies = policies

    def refresh_policies(self, policies):
        batch = {}
        for entry in policies:
            policy = _Policy(
                entry["domain"],
                entry.get("mode", "enforce"),
                entry.get("mx_patterns", ("*",)),
                entry.get("max_age", 3600),
                entry.get("created_at", 0.0),
            )
            batch[policy.domain] = policy
        with self._lock:
            self._policies = batch

    def evaluate(
        self,
        domain,
        mx_host,
        recipients,
        capabilities=(),
        cert_name=None,
        dane_valid=True,
        now=0.0,
        prior_capabilities=None,
        smtp_status=None,
        pipelined_before_tls=False,
    ):
        caps = self._sanitize_capabilities(capabilities)
        starttls_offered = "STARTTLS" in caps
        prior_caps = self._sanitize_capabilities(prior_capabilities or ())
        starttls_stripped = "STARTTLS" in prior_caps and not starttls_offered

        if pipelined_before_tls:
            return self._verdict(
                "queue", "PIPELINING_DENIED", domain, mx_host, recipients, reason="connection"
            )

        if smtp_status is not None:
            if 400 <= smtp_status < 500:
                return self._verdict(
                    "queue", "TEMP_FAILURE", domain, mx_host, recipients, reason="connection"
                )
            if smtp_status >= 500:
                return self._verdict(
                    "reject",
                    "PERM_FAILURE",
                    domain,
                    mx_host,
                    recipients,
                    reason="connection",
                    rcpt_code=smtp_status,
                )

        if not dane_valid:
            return self._verdict(
                "queue", "DANE_INVALID", domain, mx_host, recipients, reason="connection"
            )

        policy = self._policy_for(domain, now)
        if policy is None:
            return self._verdict("deliver", None, domain, mx_host, recipients, plaintext=True)

        failure = None
        if not policy.mx_matches(mx_host):
            failure = ("reject", "550 5.7.1", "policy", 550)
        elif not starttls_offered:
            reason = "connection" if starttls_stripped else "policy"
            failure = ("queue", "STARTTLS_REQUIRED", reason, None)
        elif not self._cert_matches(cert_name, mx_host):
            failure = ("queue", "CERT_MISMATCH", "policy", None)

        if failure is not None:
            action, code, reason, rcpt_code = failure
            if policy.mode == "enforce":
                return self._verdict(
                    action,
                    code,
                    domain,
                    mx_host,
                    recipients,
                    reason=reason,
                    rcpt_code=rcpt_code,
                    would_fail=True,
                )
            result = self._verdict(
                "deliver",
                None,
                domain,
                mx_host,
                recipients,
                plaintext=not starttls_offered,
                would_fail=True,
            )
            return result

        return self._verdict("deliver", None, domain, mx_host, recipients, plaintext=False)

    def evaluate_mx(
        self,
        domain,
        mx_hosts,
        recipients,
        capabilities=(),
        cert_name=None,
        dane_valid=True,
        now=0.0,
        smtp_status=None,
    ):
        attempted = []
        last = None
        for host in mx_hosts:
            attempted.append(host)
            result = self.evaluate(
                domain,
                host,
                recipients,
                capabilities=capabilities,
                cert_name=cert_name,
                dane_valid=dane_valid,
                now=now,
                smtp_status=smtp_status,
            )
            result["attempted_mx"] = list(attempted)
            if result["action"] in ("deliver", "reject"):
                return result
            last = result
        if last is None:
            return self._verdict(
                "queue", "NO_MX", domain, None, recipients, reason="connection"
            )
        last["plaintext"] = False
        return last

    def reports(self):
        with self._lock:
            return list(self._reports)

    @staticmethod
    def _sanitize_capabilities(capabilities):
        clean = set()
        for cap in capabilities:
            cap = str(cap)
            if _CAPABILITY_RE.match(cap):
                clean.add(cap.upper())
        return clean

    @staticmethod
    def _cert_matches(cert_name, mx_host):
        if not cert_name:
            return False
        return fnmatch.fnmatchcase(mx_host.lower(), str(cert_name).lower())

    def _policy_for(self, domain, now):
        policies = self._policies
        policy = policies.get(domain.lower())
        if policy is None or not policy.is_live(now):
            return None
        return policy

    def _verdict(
        self,
        action,
        code,
        domain,
        mx_host,
        recipients,
        plaintext=False,
        reason=None,
        rcpt_code=None,
        would_fail=False,
    ):
        result = {
            "action": action,
            "code": code,
            "plaintext": plaintext,
            "domain": domain,
            "mx_host": mx_host,
            "reason": reason,
            "rcpt_code": rcpt_code,
            "would_fail": would_fail,
        }
        report = {
            "domain": domain,
            "mx_host": mx_host,
            "recipients": tuple(recipients),
            "action": action,
            "code": code,
            "reason": reason,
        }
        with self._lock:
            if report not in self._reports:
                self._reports.append(report)
        return result
