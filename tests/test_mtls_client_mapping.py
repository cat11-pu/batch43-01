import pytest

import mtls_client_mapping


def _mapping():
    mapping = mtls_client_mapping.MtlsClientMapping()
    mapping.register_tenant("tenant-a", "*.tenant-a.example")
    mapping.register_tenant("tenant-b", "*.tenant-b.example")
    mapping.register_tenant("tenant-shared", "*.shared.example")
    return mapping


def _cert(cn="api.tenant-a.example", san=("api.tenant-a.example",),
          not_after=2000, serial="S1", fingerprint="F1", chain_complete=True,
          revoked=False, ocsp_status="good"):
    return {
        "cn": cn,
        "san": list(san),
        "not_after": not_after,
        "serial": serial,
        "fingerprint": fingerprint,
        "chain_complete": chain_complete,
        "revoked": revoked,
        "ocsp_status": ocsp_status,
    }


def test_san_takes_precedence_over_cn():
    mapping = _mapping()
    cert = _cert(cn="cn.tenant-b.example",
                 san=("api.tenant-a.example",))
    assert mapping.extract_identity(cert) == "api.tenant-a.example"
    result = mapping.map_certificate(cert, "tenant-a")
    assert result["tenant"] == "tenant-a"


def test_san_outside_tenant_pattern_is_rejected():
    mapping = _mapping()
    cert = _cert(san=("api.other.example",))
    with pytest.raises(mtls_client_mapping.MtlsError) as exc:
        mapping.map_certificate(cert, "tenant-a")
    assert exc.value.code == "MTLS_IDENTITY_MISMATCH"


def test_wildcard_covers_exactly_one_label():
    mapping = _mapping()
    assert mapping.identity_matches("*.example.com", "a.example.com") is True
    assert mapping.identity_matches("*.example.com", "a.b.example.com") is False


def test_expired_certificate_has_no_grace_period():
    mapping = _mapping()
    cert = _cert(not_after=500)
    with pytest.raises(mtls_client_mapping.MtlsError) as exc:
        mapping.map_certificate(cert, "tenant-a", now=1000)
    assert exc.value.code == "MTLS_CERT_EXPIRED"


def test_revoked_serial_is_rejected():
    mapping = _mapping()
    mapping.revoke("S1")
    with pytest.raises(mtls_client_mapping.MtlsError) as exc:
        mapping.map_certificate(_cert(serial="S1"), "tenant-a")
    assert exc.value.code == "MTLS_REVOKED"


def test_ocsp_timeout_fails_closed():
    mapping = _mapping()
    cert = _cert(ocsp_status="timeout")
    with pytest.raises(mtls_client_mapping.MtlsError) as exc:
        mapping.map_certificate(cert, "tenant-a")
    assert exc.value.code == "MTLS_REVOKED"


def test_incomplete_chain_is_rejected():
    mapping = _mapping()
    cert = _cert(chain_complete=False)
    with pytest.raises(mtls_client_mapping.MtlsError) as exc:
        mapping.map_certificate(cert, "tenant-a")
    assert exc.value.code == "MTLS_CHAIN_INCOMPLETE"


def test_certificate_cannot_map_to_two_tenants():
    mapping = _mapping()
    mapping.register_tenant("tenant-alias", "*.shared.example")
    cert = _cert(san=("api.shared.example",))
    assert mapping.map_certificate(cert, "tenant-shared")["tenant"] == \
        "tenant-shared"
    with pytest.raises(mtls_client_mapping.MtlsError) as exc:
        mapping.map_certificate(cert, "tenant-alias")
    assert exc.value.code == "MTLS_MAPPING_CONFLICT"


def test_duplicate_serial_across_certificates_raises_alarm():
    mapping = _mapping()
    mapping.map_certificate(_cert(serial="S9", fingerprint="F9"), "tenant-a")
    other = _cert(serial="S9", fingerprint="F10")
    with pytest.raises(mtls_client_mapping.MtlsError) as exc:
        mapping.map_certificate(other, "tenant-a")
    assert exc.value.code == "MTLS_SERIAL_REUSE"
    assert "MTLS_SERIAL_REUSE" in mapping.alarms()


def test_injected_identity_header_is_ignored():
    mapping = _mapping()
    cert = _cert()
    headers = {"X-Client-Id": "tenant-b", "X-Forwarded-User": "mallory"}
    assert mapping.upstream_identity(headers, cert) == \
        "api.tenant-a.example"


def test_session_resume_keeps_certificate_identity():
    mapping = _mapping()
    cert = _cert()
    session = mapping.start_session(cert, "tenant-a")
    resumed = mapping.resume_session(session["session_id"], cert)
    assert resumed["identity"] == "api.tenant-a.example"
    assert resumed["tenant"] == "tenant-a"


def test_session_resume_cannot_switch_identity():
    mapping = _mapping()
    first = _cert(serial="S1", fingerprint="F1")
    second = _cert(san=("api2.tenant-a.example",), serial="S2",
                   fingerprint="F2")
    session = mapping.start_session(first, "tenant-a")
    with pytest.raises(mtls_client_mapping.MtlsError) as exc:
        mapping.resume_session(session["session_id"], second)
    assert exc.value.code == "MTLS_IDENTITY_MISMATCH"


def test_repeated_mapping_is_consistent():
    mapping = _mapping()
    cert = _cert()
    first = mapping.map_certificate(cert, "tenant-a")
    second = mapping.map_certificate(cert, "tenant-a")
    assert first == second
    assert mapping.alarms() == []


def test_identity_is_not_authorized_for_other_tenant():
    mapping = _mapping()
    cert = _cert()
    identity = mapping.map_certificate(cert, "tenant-a")["identity"]
    assert mapping.authorize(identity, "tenant-a") is True
    assert mapping.authorize(identity, "tenant-b") is False


def test_valid_certificate_maps_to_tenant():
    mapping = _mapping()
    result = mapping.map_certificate(_cert(), "tenant-a")
    assert result == {"tenant": "tenant-a",
                      "identity": "api.tenant-a.example", "serial": "S1"}
