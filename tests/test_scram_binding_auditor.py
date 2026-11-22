import pytest

import scram_binding_auditor


CHAIN = [b"intermediate-cert", b"leaf-cert"]


def _auditor(**kwargs):
    return scram_binding_auditor.ScramBindingAuditor(**kwargs)


def _begin(auditor, connection_id="conn-1", username="alice", client_nonce="client-abc", **kwargs):
    return auditor.begin(
        connection_id,
        username,
        client_nonce,
        "server-xyz",
        cert_chain=kwargs.pop("cert_chain", CHAIN),
        **kwargs,
    )


def test_channel_binding_mismatch_rejected_without_fallback():
    auditor = _auditor()
    with pytest.raises(scram_binding_auditor.ScramError) as exc:
        _begin(auditor, channel_binding=b"wrong-binding")
    assert exc.value.code == "SCRAM_CHANNEL_BINDING_MISMATCH"
    assert auditor.counters()["plain_auth_attempts"] == 0


def test_channel_binding_uses_leaf_not_intermediate_certificate():
    auditor = _auditor()
    _begin(auditor, connection_id="leaf-ok", channel_binding=b"leaf-cert")
    with pytest.raises(scram_binding_auditor.ScramError) as exc:
        _begin(
            auditor,
            connection_id="leaf-bad",
            client_nonce="client-def",
            channel_binding=b"intermediate-cert",
        )
    assert exc.value.code == "SCRAM_CHANNEL_BINDING_MISMATCH"


def test_missing_channel_binding_on_plus_mechanism_is_rejected():
    auditor = _auditor()
    with pytest.raises(scram_binding_auditor.ScramError) as exc:
        _begin(auditor, channel_binding=None)
    assert exc.value.code == "SCRAM_CHANNEL_BINDING_REQUIRED"


def test_client_final_nonce_must_extend_combined_nonce():
    auditor = _auditor()
    _begin(auditor, channel_binding=b"leaf-cert")
    with pytest.raises(scram_binding_auditor.ScramError) as exc:
        auditor.finish("conn-1", "server-xyzclient-abc")
    assert exc.value.code == "SCRAM_NONCE_INVALID"


def test_iteration_count_below_minimum_rejected():
    auditor = _auditor(min_iterations=4096)
    with pytest.raises(scram_binding_auditor.ScramError) as exc:
        _begin(auditor, channel_binding=b"leaf-cert", iterations=4095)
    assert exc.value.code == "SCRAM_ITERATIONS_TOO_LOW"


def test_empty_salt_rejected():
    auditor = _auditor()
    with pytest.raises(scram_binding_auditor.ScramError) as exc:
        _begin(auditor, channel_binding=b"leaf-cert", salt=b"")
    assert exc.value.code == "SCRAM_SALT_INVALID"


def test_invalid_server_signature_does_not_authenticate_connection():
    auditor = _auditor()
    _begin(auditor, channel_binding=b"leaf-cert")
    result = auditor.finish("conn-1", "client-abcserver-xyz", server_signature_valid=False)
    assert result["authenticated"] is False
    assert result["code"] == "SCRAM_SIGNATURE_INVALID"
    assert auditor.is_authenticated("conn-1") is False


def test_authzid_for_another_user_denied():
    auditor = _auditor()
    with pytest.raises(scram_binding_auditor.ScramError) as exc:
        _begin(auditor, channel_binding=b"leaf-cert", authzid="bob")
    assert exc.value.code == "SCRAM_AUTHZID_DENIED"


def test_authzid_equal_to_username_allowed():
    auditor = _auditor()
    _begin(auditor, channel_binding=b"leaf-cert", authzid="alice")
    result = auditor.finish("conn-1", "client-abcserver-xyz")
    assert result["authenticated"] is True


def test_client_final_message_replay_on_same_connection_rejected():
    auditor = _auditor()
    _begin(auditor, channel_binding=b"leaf-cert")
    auditor.finish("conn-1", "client-abcserver-xyz")
    with pytest.raises(scram_binding_auditor.ScramError) as exc:
        auditor.finish("conn-1", "client-abcserver-xyz")
    assert exc.value.code == "SCRAM_REPLAY"


def test_client_final_message_replay_across_connections_rejected():
    auditor = _auditor()
    _begin(auditor, connection_id="conn-1", channel_binding=b"leaf-cert")
    auditor.finish("conn-1", "client-abcserver-xyz")
    _begin(auditor, connection_id="conn-2", channel_binding=b"leaf-cert")
    with pytest.raises(scram_binding_auditor.ScramError) as exc:
        auditor.finish("conn-2", "client-abcserver-xyz")
    assert exc.value.code == "SCRAM_REPLAY"


def test_reused_client_nonce_rejected():
    auditor = _auditor()
    _begin(auditor, connection_id="conn-1", channel_binding=b"leaf-cert")
    with pytest.raises(scram_binding_auditor.ScramError) as exc:
        _begin(auditor, connection_id="conn-2", channel_binding=b"leaf-cert")
    assert exc.value.code == "SCRAM_NONCE_REUSE"


def test_key_derivation_version_mismatch_rejected():
    auditor = _auditor()
    with pytest.raises(scram_binding_auditor.ScramError) as exc:
        _begin(
            auditor,
            channel_binding=b"leaf-cert",
            stored_key_version=2,
            derived_key_version=1,
        )
    assert exc.value.code == "SCRAM_KEY_VERSION"


def test_successful_channel_bound_authentication():
    auditor = _auditor()
    context = _begin(auditor, channel_binding=b"leaf-cert")
    assert context["mechanism"] == "SCRAM-SHA-256-PLUS"
    result = auditor.finish("conn-1", "client-abcserver-xyz")
    assert result["authenticated"] is True
    assert auditor.is_authenticated("conn-1") is True


def test_binding_mismatch_never_marks_connection_authenticated():
    auditor = _auditor()
    with pytest.raises(scram_binding_auditor.ScramError):
        _begin(auditor, channel_binding=b"intermediate-cert")
    assert auditor.is_authenticated("conn-1") is False
    assert auditor.counters()["authenticated_connections"] == 0
