import pytest

import macsec_key_agreement


def _elected(**kwargs):
    mka = macsec_key_agreement.MacsecKeyAgreement(**kwargs)
    mka.elect_key_server([
        {"id": "sw-a", "priority": 10, "sci": "00:11"},
        {"id": "sw-b", "priority": 20, "sci": "00:09"},
    ])
    return mka


def test_sak_install_before_election_is_rejected():
    mka = macsec_key_agreement.MacsecKeyAgreement()
    with pytest.raises(macsec_key_agreement.MacsecError) as exc:
        mka.install_sak("sci-1", "sak-1", b"key-1")
    assert exc.value.code == "MACSEC_ELECTION_INCOMPLETE"


def test_election_tie_breaks_deterministically_on_sci():
    mka = macsec_key_agreement.MacsecKeyAgreement()
    candidates = [
        {"id": "sw-a", "priority": 10, "sci": "00:11"},
        {"id": "sw-b", "priority": 10, "sci": "00:09"},
    ]
    first = mka.elect_key_server(candidates)
    second = mka.elect_key_server(candidates)
    assert first["elected"] == "sw-b"
    assert first["elected"] == second["elected"]
    assert first["participants"] == 2


def test_second_sak_is_rejected_and_first_stays_active():
    mka = _elected()
    mka.install_sak("sci-1", "sak-1", b"key-1")
    with pytest.raises(macsec_key_agreement.MacsecError) as exc:
        mka.install_sak("sci-1", "sak-2", b"key-2")
    assert exc.value.code == "MACSEC_SAK_CONFLICT"
    assert mka.active_sak("sci-1") == "sak-1"


def test_reinstalling_same_sak_is_idempotent():
    mka = _elected()
    first = mka.install_sak("sci-1", "sak-1", b"key-1")
    second = mka.install_sak("sci-1", "sak-1", b"key-1")
    assert first["active_sak"] == second["active_sak"] == "sak-1"
    assert second["installed"] is True


def test_invalid_icv_is_dropped():
    mka = _elected()
    with pytest.raises(macsec_key_agreement.MacsecError) as exc:
        mka.verify_icv(b"frame", icv_valid=False)
    assert exc.value.code == "MKA_ICV_INVALID"


def test_pn_replay_within_sa_is_rejected():
    mka = _elected()
    mka.install_sak("sci-1", "sak-1", b"key-1")
    assert mka.check_pn("sak-1", 5)["accepted"] is True
    result = mka.check_pn("sak-1", 4)
    assert result["accepted"] is False
    assert result["code"] == "MACSEC_REPLAY"


def test_pn_counters_are_per_security_association():
    mka = _elected()
    mka.install_sak("sci-1", "sak-1", b"key-1")
    mka.install_sak("sci-2", "sak-2", b"key-2")
    mka.check_pn("sak-1", 10)
    assert mka.check_pn("sak-2", 1)["accepted"] is True


def test_pn_exhaustion_forces_rekey_and_rejects_old_sak():
    mka = _elected(pn_limit=8)
    mka.install_sak("sci-1", "sak-1", b"key-1")
    assert mka.check_pn("sak-1", 8)["accepted"] is True
    exhausted = mka.check_pn("sak-1", 9)
    assert exhausted["accepted"] is False
    assert exhausted["code"] == "MACSEC_SAK_EXHAUSTED"
    assert mka.rekey_required("sci-1") is True
    mka.rekey("sci-1", "sak-2")
    assert mka.active_sak("sci-1") == "sak-2"
    assert mka.check_pn("sak-1", 1)["code"] == "MACSEC_SAK_EXHAUSTED"
    assert mka.check_pn("sak-2", 1)["accepted"] is True


def test_cipher_suite_mismatch_is_not_downgraded():
    mka = _elected()
    with pytest.raises(macsec_key_agreement.MacsecError) as exc:
        mka.negotiate_cipher("AES-GCM-256", "AES-GCM-128")
    assert exc.value.code == "MACSEC_CIPHER_MISMATCH"


def test_same_sak_material_across_connectivity_associations_is_rejected():
    mka = _elected()
    mka.bind_sak("ca-1", "sak-1", b"same-material")
    with pytest.raises(macsec_key_agreement.MacsecError) as exc:
        mka.bind_sak("ca-2", "sak-2", b"same-material")
    assert exc.value.code == "MACSEC_SAK_REUSE"


def test_invalid_confidentiality_offset_is_rejected():
    mka = _elected()
    with pytest.raises(macsec_key_agreement.MacsecError) as exc:
        mka.validate_offset(10, 64)
    assert exc.value.code == "MACSEC_OFFSET_INVALID"


def test_restore_without_cak_fails_closed():
    mka = macsec_key_agreement.MacsecKeyAgreement()
    with pytest.raises(macsec_key_agreement.MacsecError) as exc:
        mka.load_state({"saks": {"sci-1": "sak-1"}})
    assert exc.value.code == "MACSEC_STATE_INCOMPLETE"


def test_restore_with_cak_continues_packet_numbers():
    mka = _elected()
    mka.set_cak(b"cak-material")
    mka.install_sak("sci-1", "sak-1", b"key-1")
    mka.check_pn("sak-1", 5)
    state = mka.dump_state()
    restarted = macsec_key_agreement.MacsecKeyAgreement()
    restarted.load_state(state)
    assert restarted.active_sak("sci-1") == "sak-1"
    assert restarted.check_pn("sak-1", 4)["code"] == "MACSEC_REPLAY"
    assert restarted.check_pn("sak-1", 6)["accepted"] is True


def test_duplicate_mka_frame_is_replay():
    mka = _elected()
    assert mka.process_mka_frame("sci-1", 1)["ok"] is True
    with pytest.raises(macsec_key_agreement.MacsecError) as exc:
        mka.process_mka_frame("sci-1", 1)
    assert exc.value.code == "MACSEC_MKA_REPLAY"


def test_stale_participant_is_removed_and_cannot_be_elected():
    mka = macsec_key_agreement.MacsecKeyAgreement(participant_timeout=300)
    mka.add_participant("sw-a", now=0)
    assert mka.timeout_participants(now=400)["removed"] == 1
    assert mka.participants() == []
    with pytest.raises(macsec_key_agreement.MacsecError) as exc:
        mka.elect_key_server([])
    assert exc.value.code == "MACSEC_ELECTION_EMPTY"
