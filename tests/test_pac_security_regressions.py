import threading

import pac_claim_verifier
from pac_claim_verifier import PacClaimVerifier


def _verifier(**kwargs):
    return PacClaimVerifier(**kwargs)


def _pac(**overrides):
    pac = {
        "issuer": "corp.example",
        "kdc_signature_valid": True,
        "service_signature_valid": True,
        "issued_at": 0.0,
        "logon_time": 0.0,
        "key_version": 2,
        "empty": False,
        "user_name": "alice",
        "groups": ["S-1-5-21-1-513"],
        "sid_history": [],
        "buffers": [{"type": 1, "offset": 0, "length": 4, "data": b"abcdefgh"}],
    }
    pac.update(overrides)
    return pac


def test_signature_failure_never_returns_pac_groups():
    pac = _pac(groups=["S-1-5-21-1-512"], kdc_signature_valid=False)
    result = _verifier().verify(pac)
    assert result["authenticated"] is False
    assert result["code"] == "PAC_SIGNATURE_INVALID"
    assert result["groups"] == []


def test_both_forests_trusted_in_coexistence():
    verifier = _verifier(trusted_forests=("corp.example", "legacy.corp.example"))
    pac = _pac(
        sid_history=[{"sid": "S-1-5-21-7-1105", "forest": "legacy.corp.example"}]
    )
    result = verifier.verify(pac)
    assert result["authenticated"] is True
    assert "S-1-5-21-7-1105" in result["groups"]


def test_pac_at_max_age_boundary_is_accepted():
    verifier = _verifier(max_age=3600.0)
    result = verifier.verify(_pac(issued_at=0.0), now=3600.0)
    assert result["authenticated"] is True


def test_pac_just_over_max_age_requires_revalidation():
    verifier = _verifier(max_age=3600.0)
    result = verifier.verify(_pac(issued_at=0.0), now=3600.1)
    assert result["authenticated"] is False
    assert result["code"] == "PAC_REVALIDATE_REQUIRED"


def test_buffer_gaps_are_alignment_padding_not_data():
    pac = _pac(
        buffers=[
            {"type": 1, "offset": 0, "length": 4, "data": b"abcdefghijkl"},
            {"type": 2, "offset": 8, "length": 4, "data": b"abcdefghijkl"},
        ]
    )
    result = _verifier().verify(pac)
    assert result["authenticated"] is True


def test_negative_offset_and_length_rejected_as_overflow():
    pac = _pac(buffers=[{"type": 1, "offset": -1, "length": 4, "data": b"abcd"}])
    assert _verifier().verify(pac)["code"] == "PAC_BUFFER_OVERFLOW"

    pac = _pac(buffers=[{"type": 1, "offset": 0, "length": -1, "data": b"abcd"}])
    assert _verifier().verify(pac)["code"] == "PAC_BUFFER_OVERFLOW"


def test_other_privileged_rids_from_untrusted_forest_rejected():
    for rid in (518, 519, 544):
        pac = _pac(
            sid_history=[
                {"sid": f"S-1-5-21-9-{rid}", "forest": "other.example"}
            ]
        )
        result = _verifier().verify(pac)
        assert result["authenticated"] is False
        assert result["code"] == "PAC_SID_FILTERED"


def test_untrusted_non_privileged_sid_history_silently_filtered():
    pac = _pac(
        sid_history=[
            {"sid": "S-1-5-21-9-1001", "forest": "other.example"},
        ]
    )
    result = _verifier().verify(pac)
    assert result["authenticated"] is True
    assert result["groups"] == ["S-1-5-21-1-513"]
    assert result["filtered_sids"] == ["S-1-5-21-9-1001"]


def test_audit_trail_records_rejected_and_accepted_decisions():
    verifier = _verifier()
    verifier.verify(_pac(kdc_signature_valid=False))
    verifier.verify(_pac())

    trail = verifier.audit_trail()
    assert [entry["code"] for entry in trail] == [
        "PAC_SIGNATURE_INVALID",
        None,
    ]
    assert trail[0]["presented_groups"] == ["S-1-5-21-1-513"]
    assert trail[0]["groups"] == []
    assert trail[1]["groups"] == ["S-1-5-21-1-513"]


def test_concurrent_pac_mutation_never_corrupts_decision():
    verifier = _verifier()
    shared_pac = _pac()
    errors = []

    def churn():
        for index in range(500):
            shared_pac["groups"] = [f"S-1-5-21-1-{1000 + index}"]
            shared_pac["buffers"] = [
                {"type": 1, "offset": 0, "length": 4, "data": b"abcdefgh"}
            ]
            shared_pac["kdc_signature_valid"] = True

    def verify_loop():
        try:
            for _ in range(500):
                result = verifier.verify(shared_pac)
                if result["authenticated"]:
                    assert result["code"] is None
                else:
                    assert result["groups"] == []
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    writer = threading.Thread(target=churn)
    reader = threading.Thread(target=verify_loop)
    writer.start()
    reader.start()
    writer.join()
    reader.join()

    assert errors == []


def test_verifier_module_exposes_stable_decision_codes():
    for code in (
        "PAC_SIGNATURE_INVALID",
        "PAC_SID_FILTERED",
        "PAC_BUFFER_OVERLAP",
        "PAC_BUFFER_OVERFLOW",
        "PAC_DUPLICATE_BUFFER",
        "PAC_EMPTY",
        "PAC_ISSUER_UNTRUSTED",
        "PAC_REVALIDATE_REQUIRED",
        "PAC_KEY_VERSION",
        "PAC_TIME_INVALID",
    ):
        assert getattr(pac_claim_verifier, code) == code
