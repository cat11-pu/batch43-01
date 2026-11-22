import pytest

import audio_device_switchboard as ads


def make(**kwargs):
    return ads.AudioDeviceSwitchboard(**kwargs)


def rig():
    s = make(sample_rate=48000, channels=2)
    s.add_device("hp", sample_rate=48000, channels=2)
    s.add_device("speaker", sample_rate=48000, channels=2)
    s.set_default("hp")
    return s


def test_unplug_falls_back_to_speaker_without_silence():
    s = rig()
    s.remove_device("hp")
    out = s.set_default("speaker")
    assert out["switched"] is True
    assert s.current() == "speaker"


def test_sample_rate_switch_preserves_playhead_seconds():
    s = rig()
    s.advance(48000)
    assert s.playhead() == pytest.approx(1.0)
    s.add_device("bt", sample_rate=44100, channels=2)
    s.set_default("bt")
    assert s.playhead() == pytest.approx(1.0)
    assert s.format()["sample_rate"] == 44100


def test_surround_channel_map_keeps_voice_out_of_lfe():
    s = make()
    s.add_device("surround", sample_rate=48000, channels=6)
    s.set_default("surround")
    channel_map = s.format()["channel_map"]
    assert channel_map["voice"] == 2
    assert channel_map["voice"] != 5


def test_duplicate_voice_registration_is_deduped():
    s = rig()
    first = s.add_voice("hp", "voice-1")
    second = s.add_voice("hp", "voice-1")
    assert first["added"] is True
    assert second["added"] is False
    assert s.voice_count("hp") == 1


def test_unhealthy_device_falls_back_to_null_sink():
    s = make()
    s.add_device("bad", healthy=False)
    out = s.set_default("bad")
    assert out["switched"] is False
    assert out["device"] == "null"
    assert s.current() == "null"


def test_old_device_keeps_its_format_during_switch():
    s = make()
    s.add_device("d1", sample_rate=48000, channels=2)
    s.add_device("d2", sample_rate=44100, channels=6)
    s.set_default("d1")
    s.set_default("d2")
    assert s.callback("d1")["sample_rate"] == 48000
    assert s.callback("d1")["channels"] == 2
    assert s.callback("d2")["sample_rate"] == 44100


def test_replug_does_not_multiply_voices():
    s = rig()
    s.add_voice("hp", "voice-1")
    s.remove_device("hp")
    s.add_device("hp", sample_rate=48000, channels=2)
    s.set_default("hp")
    s.add_voice("hp", "voice-1")
    assert s.voice_count("hp") == 1


def test_latency_change_recomputes_compensation_samples():
    s = rig()
    out = s.set_audio_latency(0.05)
    assert out["compensation_samples"] == 2400
    assert out["sample_rate"] == 48000


def test_lost_exclusive_mode_keeps_audio_running():
    s = make()
    s.add_device("d1", exclusive=True)
    s.set_default("d1")
    out = s.lose_exclusive()
    assert out["continued"] is True
    assert s.current() in ("d1", "null")
    assert s.playhead() >= 0.0


def test_playhead_is_monotonic_across_format_changes():
    s = rig()
    s.advance(4800)
    before = s.playhead()
    s.add_device("bt", sample_rate=44100)
    s.set_default("bt")
    after = s.playhead()
    assert after >= before
    s.advance(4410)
    assert s.playhead() >= after


def test_removed_device_callback_is_rejected():
    s = rig()
    s.remove_device("hp")
    with pytest.raises(ads.AudioError) as exc:
        s.callback("hp")
    assert exc.value.code == "DEVICE_RELEASED"


def test_rapid_hotplug_does_not_duplicate_devices():
    s = make()
    for _ in range(3):
        s.add_device("d1", sample_rate=48000)
        s.set_default("d1")
        s.remove_device("d1")
    s.add_device("d1", sample_rate=48000)
    assert s.active_devices().count("d1") <= 1


def test_callback_only_works_for_active_devices():
    s = rig()
    s.set_default("speaker")
    s.drain("hp")
    with pytest.raises(ads.AudioError) as exc:
        s.callback("hp")
    assert exc.value.code == "DEVICE_RELEASED"


def test_format_migration_is_atomic_per_device():
    s = make()
    s.add_device("d1", sample_rate=48000, channels=2)
    s.add_device("d2", sample_rate=44100, channels=6)
    s.set_default("d1")
    s.set_default("d2")
    assert s.callback("d1")["channels"] == 2
    assert s.callback("d2")["channels"] == 6
    assert s.format("d1")["sample_rate"] == 48000
    assert s.format("d2")["sample_rate"] == 44100


def test_voice_counts_are_scoped_per_device():
    s = make()
    s.add_device("d1", sample_rate=48000)
    s.add_device("d2", sample_rate=48000)
    s.add_voice("d1", "voice-1")
    s.add_voice("d2", "voice-2")
    s.add_voice("d2", "voice-3")
    assert s.voice_count("d1") == 1
    assert s.voice_count("d2") == 2
