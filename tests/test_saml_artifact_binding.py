import pytest

import saml_artifact_binding


def _assertion(assertion_id="a-1", audience=("sp.example",), signed=True,
               not_before=0, not_on_or_after=2000):
    return {
        "assertion_id": assertion_id,
        "audience": list(audience),
        "assertion_signed": signed,
        "not_before": not_before,
        "not_on_or_after": not_on_or_after,
        "subject": "alice",
    }


def test_artifact_can_only_be_resolved_once():
    saml = saml_artifact_binding.SamlArtifactBinding()
    request = saml.issue_request("sp.example", relay_state="/app")
    artifact = saml.create_artifact(_assertion(), "sp.example")["artifact"]
    first = saml.resolve_artifact(artifact, "sp.example",
                                  request["request_id"])
    assert first["session_id"]
    with pytest.raises(saml_artifact_binding.SamlError) as exc:
        saml.resolve_artifact(artifact, "sp.example", request["request_id"])
    assert exc.value.code == "SAML_ARTIFACT_REPLAY"


def test_artifact_is_bound_to_its_service_provider():
    saml = saml_artifact_binding.SamlArtifactBinding()
    request = saml.issue_request("other.example")
    artifact = saml.create_artifact(_assertion(audience=("other.example",)),
                                    "other.example")["artifact"]
    with pytest.raises(saml_artifact_binding.SamlError) as exc:
        saml.resolve_artifact(artifact, "sp.example", request["request_id"])
    assert exc.value.code == "SAML_ARTIFACT_BINDING"


def test_missing_request_id_is_rejected():
    saml = saml_artifact_binding.SamlArtifactBinding()
    artifact = saml.create_artifact(_assertion(), "sp.example")["artifact"]
    with pytest.raises(saml_artifact_binding.SamlError) as exc:
        saml.resolve_artifact(artifact, "sp.example", None)
    assert exc.value.code == "SAML_REQUEST_UNKNOWN"


def test_wrong_request_id_is_rejected():
    saml = saml_artifact_binding.SamlArtifactBinding()
    saml.issue_request("sp.example")
    artifact = saml.create_artifact(_assertion(), "sp.example")["artifact"]
    with pytest.raises(saml_artifact_binding.SamlError) as exc:
        saml.resolve_artifact(artifact, "sp.example", "bogus-request")
    assert exc.value.code == "SAML_REQUEST_UNKNOWN"


def test_external_relay_state_is_rejected():
    saml = saml_artifact_binding.SamlArtifactBinding()
    with pytest.raises(saml_artifact_binding.SamlError) as exc:
        saml.validate_relay("https://evil.example/phish")
    assert exc.value.code == "SAML_RELAY_INVALID"


def test_relative_relay_state_is_allowed_and_preserved():
    saml = saml_artifact_binding.SamlArtifactBinding()
    assert saml.validate_relay("/dashboard")["valid"] is True
    request = saml.issue_request("sp.example", relay_state="/dashboard")
    artifact = saml.create_artifact(_assertion(), "sp.example")["artifact"]
    result = saml.resolve_artifact(artifact, "sp.example",
                                   request["request_id"])
    assert result["relay_state"] == "/dashboard"


def test_audience_mismatch_is_rejected():
    saml = saml_artifact_binding.SamlArtifactBinding()
    request = saml.issue_request("sp.example")
    artifact = saml.create_artifact(
        _assertion(audience=("someone-else.example",)), "sp.example")["artifact"]
    with pytest.raises(saml_artifact_binding.SamlError) as exc:
        saml.resolve_artifact(artifact, "sp.example", request["request_id"])
    assert exc.value.code == "SAML_AUDIENCE_INVALID"


def test_unsigned_assertion_is_rejected():
    saml = saml_artifact_binding.SamlArtifactBinding()
    request = saml.issue_request("sp.example")
    artifact = saml.create_artifact(_assertion(signed=False),
                                    "sp.example")["artifact"]
    with pytest.raises(saml_artifact_binding.SamlError) as exc:
        saml.resolve_artifact(artifact, "sp.example", request["request_id"])
    assert exc.value.code == "SAML_SIGNATURE_MISSING"


def test_expired_assertion_beyond_skew_is_rejected():
    saml = saml_artifact_binding.SamlArtifactBinding(clock_skew=120)
    request = saml.issue_request("sp.example")
    artifact = saml.create_artifact(
        _assertion(not_on_or_after=1000), "sp.example")["artifact"]
    with pytest.raises(saml_artifact_binding.SamlError) as exc:
        saml.resolve_artifact(artifact, "sp.example", request["request_id"],
                              now=1200)
    assert exc.value.code == "SAML_ASSERTION_EXPIRED"


def test_assertion_within_clock_skew_is_accepted():
    saml = saml_artifact_binding.SamlArtifactBinding(clock_skew=120)
    request = saml.issue_request("sp.example")
    artifact = saml.create_artifact(
        _assertion(not_on_or_after=1000), "sp.example")["artifact"]
    result = saml.resolve_artifact(artifact, "sp.example",
                                   request["request_id"], now=1100)
    assert result["session_id"]


def test_assertion_id_replay_survives_restart():
    saml = saml_artifact_binding.SamlArtifactBinding()
    request = saml.issue_request("sp.example")
    artifact = saml.create_artifact(_assertion(assertion_id="a-9"),
                                    "sp.example")["artifact"]
    saml.resolve_artifact(artifact, "sp.example", request["request_id"])
    state = saml.dump_state()
    restarted = saml_artifact_binding.SamlArtifactBinding()
    restarted.load_state(state)
    new_request = restarted.issue_request("sp.example")
    new_artifact = restarted.create_artifact(
        _assertion(assertion_id="a-9"), "sp.example")["artifact"]
    with pytest.raises(saml_artifact_binding.SamlError) as exc:
        restarted.resolve_artifact(new_artifact, "sp.example",
                                   new_request["request_id"])
    assert exc.value.code == "SAML_ASSERTION_REPLAY"


def test_entity_ids_are_case_insensitive():
    saml = saml_artifact_binding.SamlArtifactBinding()
    request = saml.issue_request("SP.Example")
    artifact = saml.create_artifact(_assertion(audience=("sp.example",)),
                                    "sp.example")["artifact"]
    result = saml.resolve_artifact(artifact, "sp.example",
                                   request["request_id"])
    assert result["session_id"]


def test_artifact_over_cleartext_transport_is_rejected():
    saml = saml_artifact_binding.SamlArtifactBinding()
    request = saml.issue_request("sp.example")
    artifact = saml.create_artifact(_assertion(), "sp.example")["artifact"]
    with pytest.raises(saml_artifact_binding.SamlError) as exc:
        saml.resolve_artifact(artifact, "sp.example", request["request_id"],
                              tls=False)
    assert exc.value.code == "SAML_INSECURE_TRANSPORT"


def test_request_id_is_single_use():
    saml = saml_artifact_binding.SamlArtifactBinding()
    request = saml.issue_request("sp.example")
    first = saml.create_artifact(_assertion(assertion_id="a-1"),
                                 "sp.example")["artifact"]
    second = saml.create_artifact(_assertion(assertion_id="a-2"),
                                  "sp.example")["artifact"]
    saml.resolve_artifact(first, "sp.example", request["request_id"])
    with pytest.raises(saml_artifact_binding.SamlError) as exc:
        saml.resolve_artifact(second, "sp.example", request["request_id"])
    assert exc.value.code == "SAML_REQUEST_UNKNOWN"


def test_idp_initiated_assertion_replay_is_rejected():
    saml = saml_artifact_binding.SamlArtifactBinding()
    first = saml.accept_idp_assertion(_assertion(assertion_id="idp-1"))
    assert first["session_id"]
    with pytest.raises(saml_artifact_binding.SamlError) as exc:
        saml.accept_idp_assertion(_assertion(assertion_id="idp-1"))
    assert exc.value.code == "SAML_ASSERTION_REPLAY"
