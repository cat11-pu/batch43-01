import smtp_starttls_fence


def _fence():
    return smtp_starttls_fence.SmtpStarttlsFence()


def test_domain_without_policy_may_deliver_plaintext():
    fence = _fence()
    result = fence.evaluate("plain.example", "mx.plain.example", ["a@plain.example"], capabilities=())
    assert result["action"] == "deliver"
    assert result["plaintext"] is True


def test_capability_removed_after_advertisement_must_queue():
    fence = _fence()
    fence.set_policy("secure.example", mode="enforce", mx_patterns=["mx.secure.example"])
    result = fence.evaluate(
        "secure.example",
        "mx.secure.example",
        ["user@secure.example"],
        capabilities=(),
        prior_capabilities=("STARTTLS",),
    )
    assert result["action"] == "queue"
    assert result["code"] == "STARTTLS_REQUIRED"
    assert result["plaintext"] is False


def test_enforced_policy_with_matching_mx_delivers_over_tls():
    fence = _fence()
    fence.set_policy("secure.example", mode="enforce", mx_patterns=["mx.secure.example"])
    result = fence.evaluate(
        "secure.example",
        "mx.secure.example",
        ["user@secure.example"],
        capabilities=("STARTTLS",),
        cert_name="mx.secure.example",
    )
    assert result["action"] == "deliver"
    assert result["plaintext"] is False


def test_enforced_policy_mx_mismatch_rejected_at_rcpt():
    fence = _fence()
    fence.set_policy("secure.example", mode="enforce", mx_patterns=["mx.secure.example"])
    result = fence.evaluate(
        "secure.example",
        "evil.example",
        ["user@secure.example"],
        capabilities=("STARTTLS",),
        cert_name="evil.example",
    )
    assert result["action"] == "reject"
    assert result["code"] == "550 5.7.1"
    assert result["rcpt_code"] == 550


def test_test_mode_does_not_reject_but_marks_would_fail():
    fence = _fence()
    fence.set_policy("testing.example", mode="testing", mx_patterns=["mx.testing.example"])
    result = fence.evaluate(
        "testing.example",
        "other.example",
        ["user@testing.example"],
        capabilities=("STARTTLS",),
        cert_name="other.example",
    )
    assert result["action"] != "reject"
    assert result["would_fail"] is True


def test_expired_policy_is_not_enforced():
    fence = _fence()
    fence.set_policy(
        "expired.example", mode="enforce", mx_patterns=["mx.expired.example"], max_age=10, created_at=0.0
    )
    result = fence.evaluate(
        "expired.example",
        "mx.expired.example",
        ["user@expired.example"],
        capabilities=(),
        now=100.0,
    )
    assert result["action"] == "deliver"
    assert result["plaintext"] is True


def test_certificate_name_mismatch_never_falls_back_to_plaintext():
    fence = _fence()
    fence.set_policy("secure.example", mode="enforce", mx_patterns=["mx.secure.example"])
    result = fence.evaluate(
        "secure.example",
        "mx.secure.example",
        ["user@secure.example"],
        capabilities=("STARTTLS",),
        cert_name="mx.other.example",
    )
    assert result["action"] in ("queue", "reject")
    assert result["code"] == "CERT_MISMATCH"
    assert result["plaintext"] is False


def test_dane_verification_bit_failure_blocks_delivery():
    fence = _fence()
    result = fence.evaluate(
        "dane.example",
        "mx.dane.example",
        ["user@dane.example"],
        capabilities=("STARTTLS",),
        cert_name="mx.dane.example",
        dane_valid=False,
    )
    assert result["action"] == "queue"
    assert result["code"] == "DANE_INVALID"
    assert result["plaintext"] is False


def test_temporary_4xx_failure_is_queued_not_rejected():
    fence = _fence()
    result = fence.evaluate(
        "temp.example",
        "mx.temp.example",
        ["user@temp.example"],
        capabilities=("STARTTLS",),
        smtp_status=451,
    )
    assert result["action"] == "queue"
    assert result["code"] == "TEMP_FAILURE"
    assert result["action"] != "reject"


def test_multiple_mx_tls_failure_never_downgrades_to_plaintext():
    fence = _fence()
    fence.set_policy("multi.example", mode="enforce", mx_patterns=["*.mail.example"])
    result = fence.evaluate_mx(
        "multi.example",
        ["mx1.mail.example", "mx2.mail.example"],
        ["user@multi.example"],
        capabilities=(),
    )
    assert result["action"] == "queue"
    assert result["plaintext"] is False
    assert result["attempted_mx"] == ["mx1.mail.example", "mx2.mail.example"]


def test_capability_with_crlf_is_not_parsed_as_starttls():
    fence = _fence()
    fence.set_policy("inject.example", mode="enforce", mx_patterns=["mx.inject.example"])
    result = fence.evaluate(
        "inject.example",
        "mx.inject.example",
        ["user@inject.example"],
        capabilities=("STARTTLS\r\nX-FAKE: 1",),
        cert_name="mx.inject.example",
    )
    assert result["action"] == "queue"
    assert result["code"] == "STARTTLS_REQUIRED"


def test_pipelining_before_tls_is_denied():
    fence = _fence()
    result = fence.evaluate(
        "pipe.example",
        "mx.pipe.example",
        ["user@pipe.example"],
        capabilities=("STARTTLS",),
        pipelined_before_tls=True,
    )
    assert result["action"] == "queue"
    assert result["code"] == "PIPELINING_DENIED"
    assert result["plaintext"] is False


def test_policy_refresh_replaces_batch_atomically():
    fence = _fence()
    fence.set_policy("old.example", mode="enforce", mx_patterns=["mx.old.example"])
    fence.refresh_policies(
        [{"domain": "new.example", "mode": "enforce", "mx_patterns": ["mx.new.example"]}]
    )
    old = fence.evaluate("old.example", "mx.old.example", ["a@old.example"], capabilities=())
    new = fence.evaluate("new.example", "wrong.example", ["a@new.example"], capabilities=("STARTTLS",))
    assert old["action"] == "deliver"
    assert old["plaintext"] is True
    assert new["action"] == "reject"


def test_mx_pattern_is_case_insensitive_wildcard():
    fence = _fence()
    fence.set_policy("wild.example", mode="enforce", mx_patterns=["*.MAIL.example"])
    result = fence.evaluate(
        "wild.example",
        "mx2.mail.example",
        ["user@wild.example"],
        capabilities=("STARTTLS",),
        cert_name="mx2.mail.example",
    )
    assert result["action"] == "deliver"
    assert result["plaintext"] is False


def test_unknown_domain_is_not_forced_to_starttls():
    fence = _fence()
    result = fence.evaluate("unknown.example", "mx.unknown.example", ["a@unknown.example"], capabilities=())
    assert result["action"] == "deliver"
    assert result["code"] is None
