import pac_claim_verifier


def _verifier(**kwargs):
    return pac_claim_verifier.PacClaimVerifier(**kwargs)


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


def test_invalid_kdc_signature_rejects_login_without_groups():
    result = _verifier().verify(_pac(kdc_signature_valid=False))
    assert result["authenticated"] is False
    assert result["code"] == "PAC_SIGNATURE_INVALID"
    assert result["groups"] == []


def test_invalid_service_signature_rejects_login():
    result = _verifier().verify(_pac(service_signature_valid=False))
    assert result["authenticated"] is False
    assert result["code"] == "PAC_SIGNATURE_INVALID"


def test_service_signature_check_cannot_be_switched_off():
    verifier = _verifier(require_service_sig=False)
    result = verifier.verify(_pac(service_signature_valid=False))
    assert result["authenticated"] is False
    assert result["code"] == "PAC_SIGNATURE_INVALID"


def test_valid_pac_keeps_trusted_forest_sid_history():
    pac = _pac(sid_history=[{"sid": "S-1-5-21-2-1105", "forest": "corp.example"}])
    result = _verifier().verify(pac)
    assert result["authenticated"] is True
    assert "S-1-5-21-2-1105" in result["groups"]


def test_untrusted_forest_sid_history_is_filtered():
    pac = _pac(
        sid_history=[
            {"sid": "S-1-5-21-9-1001", "forest": "other.example"},
            {"sid": "S-1-5-21-2-1105", "forest": "corp.example"},
        ]
    )
    result = _verifier().verify(pac)
    assert result["authenticated"] is True
    assert "S-1-5-21-9-1001" not in result["groups"]
    assert "S-1-5-21-2-1105" in result["groups"]


def test_untrusted_domain_admin_sid_history_is_reported():
    pac = _pac(sid_history=[{"sid": "S-1-5-21-9-512", "forest": "other.example"}])
    result = _verifier().verify(pac)
    assert result["authenticated"] is False
    assert result["code"] == "PAC_SID_FILTERED"


def test_overlapping_buffers_rejected():
    pac = _pac(
        buffers=[
            {"type": 1, "offset": 0, "length": 8, "data": b"0123456789abcdef"},
            {"type": 2, "offset": 4, "length": 8, "data": b"0123456789abcdef"},
        ]
    )
    result = _verifier().verify(pac)
    assert result["authenticated"] is False
    assert result["code"] == "PAC_BUFFER_OVERLAP"


def test_buffer_length_beyond_data_rejected():
    pac = _pac(buffers=[{"type": 1, "offset": 0, "length": 100, "data": b"abcd"}])
    result = _verifier().verify(pac)
    assert result["authenticated"] is False
    assert result["code"] == "PAC_BUFFER_OVERFLOW"


def test_duplicate_buffer_type_rejected():
    pac = _pac(
        buffers=[
            {"type": 1, "offset": 0, "length": 4, "data": b"abcdefgh"},
            {"type": 1, "offset": 4, "length": 4, "data": b"abcdefgh"},
        ]
    )
    result = _verifier().verify(pac)
    assert result["authenticated"] is False
    assert result["code"] == "PAC_DUPLICATE_BUFFER"


def test_empty_pac_is_rejected():
    result = _verifier().verify(_pac(empty=True, groups=[], buffers=[]))
    assert result["authenticated"] is False
    assert result["code"] == "PAC_EMPTY"


def test_untrusted_issuer_rejected():
    result = _verifier().verify(_pac(issuer="rogue.example"))
    assert result["authenticated"] is False
    assert result["code"] == "PAC_ISSUER_UNTRUSTED"


def test_stale_pac_requires_revalidation():
    result = _verifier().verify(_pac(issued_at=0.0), now=100000.0)
    assert result["authenticated"] is False
    assert result["code"] == "PAC_REVALIDATE_REQUIRED"


def test_unknown_key_version_rejected():
    result = _verifier().verify(_pac(key_version=9))
    assert result["authenticated"] is False
    assert result["code"] == "PAC_KEY_VERSION"


def test_future_logon_timestamp_rejected():
    result = _verifier().verify(_pac(logon_time=5000.0), now=0.0)
    assert result["authenticated"] is False
    assert result["code"] == "PAC_TIME_INVALID"


def test_valid_pac_authenticates_with_aligned_buffer():
    pac = _pac(buffers=[{"type": 1, "offset": 4, "length": 4, "data": b"abcdefgh"}])
    result = _verifier().verify(pac, now=10.0)
    assert result["authenticated"] is True
    assert result["groups"] == ["S-1-5-21-1-513"]
    assert result["code"] is None
