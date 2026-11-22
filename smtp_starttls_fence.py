"""SMTP STARTTLS policy fence and delivery gate (skeleton)."""


class SmtpError(Exception):
    """Configuration failure raised by the fence."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class SmtpStarttlsFence:
    """Evaluates MTA-STS style policies before any plaintext delivery attempt."""

    def __init__(self):
        pass

    def set_policy(self, domain, mode="enforce", mx_patterns=("*",), max_age=3600, created_at=0.0):
        raise NotImplementedError

    def refresh_policies(self, policies):
        raise NotImplementedError

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
        raise NotImplementedError

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
        raise NotImplementedError

    def reports(self):
        raise NotImplementedError
