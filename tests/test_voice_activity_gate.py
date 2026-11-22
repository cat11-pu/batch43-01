import pytest

import voice_activity_gate as vag


def make(**kwargs):
    return vag.VoiceActivityGate(**kwargs)


def test_level_below_threshold_is_silent():
    p = make(threshold_db=-40)
    out = p.detect("u1", -50.0, 0)
    assert out["state"] == "silent"
    assert p.gate("u1", 0)["open"] is False


def test_onset_opens_on_the_same_frame_with_full_gain():
    p = make()
    out = p.detect("u1", -20.0, 100)
    assert out["state"] == "speaking"
    assert out["onset"] is True
    gate = p.gate("u1", 100)
    assert gate["open"] is True
    assert gate["gain"] == 1.0


def test_hangover_holds_the_gate_open():
    p = make(hangover_ms=300)
    p.detect("u1", -20.0, 0)
    p.detect("u1", -60.0, 100)
    assert p.gate("u1", 200)["open"] is True
    assert p.gate("u1", 250)["open"] is True
    assert p.gate("u1", 350)["open"] is False


def test_gate_closes_after_hangover_without_new_frames():
    p = make(hangover_ms=300)
    p.detect("u1", -20.0, 0)
    assert p.gate("u1", 1000)["open"] is False
    assert p.gate("u1", 1001)["gain"] == 0.0


def test_push_to_talk_opens_while_quiet():
    p = make()
    out = p.detect("u1", -80.0, 0, push_to_talk=True)
    assert out["state"] == "speaking"
    assert p.gate("u1", 0)["open"] is True


def test_push_to_talk_release_closes_immediately():
    p = make(hangover_ms=300)
    p.detect("u1", -20.0, 0, push_to_talk=True)
    out = p.detect("u1", -80.0, 10, push_to_talk=False)
    assert out["state"] == "silent"
    assert p.gate("u1", 20)["open"] is False


def test_each_user_has_a_single_detection_state():
    p = make()
    p.detect("u1", -20.0, 0)
    p.detect("u1", -21.0, 10)
    p.detect("u1", -22.0, 20)
    assert p.active_users() == ["u1"]
    assert p.state("u1")["state"] == "speaking"


def test_two_speakers_are_independent():
    p = make()
    p.detect("u1", -20.0, 0)
    p.detect("u2", -60.0, 0)
    assert p.active_users() == ["u1"]
    assert p.gate("u1", 0)["open"] is True
    assert p.gate("u2", 0)["open"] is False


def test_mute_closes_the_gate_even_with_speech():
    p = make()
    p.set_mute("u1", True)
    out = p.detect("u1", -10.0, 0)
    assert out["state"] == "muted"
    assert p.gate("u1", 0)["open"] is False
    assert p.gate("u1", 0)["gain"] == 0.0


def test_muting_one_user_does_not_affect_another():
    p = make()
    p.detect("u1", -30.0, 0)
    p.detect("u2", -30.0, 0)
    p.set_mute("u1", True)
    assert p.gate("u1", 0)["open"] is False
    assert p.gate("u2", 0)["open"] is True


def test_threshold_behaviour_is_identical_across_sample_rates():
    p = make(threshold_db=-40)
    low48 = p.detect("u48", -45.0, 0, sample_rate=48000)["state"]
    low16 = p.detect("u16", -45.0, 0, sample_rate=16000)["state"]
    high48 = p.detect("u48", -20.0, 10, sample_rate=48000)["state"]
    high16 = p.detect("u16", -20.0, 10, sample_rate=16000)["state"]
    assert low48 == low16 == "silent"
    assert high48 == high16 == "speaking"


def test_ducking_lowers_game_and_music_but_not_voice():
    p = make(duck_db=12)
    out = p.duck({"game": 0.0, "music": 0.0, "voice": 0.0}, active=True)
    assert out["game"] == -12
    assert out["music"] == -12
    assert out["voice"] == 0.0


def test_ducking_releases_when_speech_stops():
    p = make(duck_db=12)
    out = p.duck({"game": -12.0, "music": -12.0, "voice": 0.0}, active=False)
    assert out["game"] == 0.0
    assert out["music"] == 0.0
    assert out["voice"] == 0.0


def test_hotplug_resets_gain_and_filter_state():
    p = make()
    p.set_device("u1", "usb-mic")
    p.detect("u1", -20.0, 0)
    out = p.hotplug("u1", "bluetooth-mic")
    assert out["device"] == "bluetooth-mic"
    assert out["gain"] == 1.0
    assert out["filter"] == "reset"
    assert p.state("u1")["device"] == "bluetooth-mic"


def test_restart_restores_settings_and_mute_state():
    p = make(hangover_ms=250, threshold_db=-35)
    p.set_mute("u1", True)
    p.set_device("u2", "headset")
    state = p.snapshot()
    q = make()
    q.load(state)
    assert q.state("u1")["muted"] is True
    assert q.state("u2")["device"] == "headset"
    assert q.detect("u1", -10.0, 0)["state"] == "muted"
