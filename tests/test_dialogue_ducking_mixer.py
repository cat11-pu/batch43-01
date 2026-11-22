import pytest

import dialogue_ducking_mixer as ddm


def make(**kwargs):
    kwargs.setdefault("sample_rate", 48000)
    return ddm.DialogueDuckingMixer(**kwargs)


def test_short_breath_blip_does_not_duck_music():
    m = make(min_speech_seconds=0.05)
    flags = [1] + [0] * 2000
    out = m.process(flags)
    assert all(gain == 1.0 for gain in out["music"])


def test_release_has_no_step_and_returns_to_baseline():
    m = make(release_seconds=0.2)
    flags = [1] * 4800 + [0] * 20000
    gains = m.process(flags)["music"]
    steps = [abs(gains[i] - gains[i - 1]) for i in range(1, len(gains))]
    assert max(steps) < 0.001
    assert abs(gains[-1] - 1.0) < 1e-9


def test_multiple_voices_merge_by_max_not_sum():
    m = make()
    voices = [[1, 0, 1, 0], [0, 0, 1, 0], [1, 0, 0, 0]]
    merged = m.merge_voices(voices)
    assert merged == [1, 0, 1, 0]
    one = make().process([1, 0, 1, 0])["music"]
    many = make().process(merged)["music"]
    assert one == many


def test_music_gain_never_goes_below_duck_floor():
    m = make(duck_gain=0.25)
    flags = [1] * 10000
    gains = m.process(flags)["music"]
    assert all(gain >= 0.25 for gain in gains)


def test_gain_returns_exactly_to_baseline_after_duck():
    m = make()
    m.process([1] * 6000)
    gains = m.process([0] * 30000)["music"]
    assert gains[-1] == 1.0


def test_snapshot_resets_a_ducked_envelope():
    m = make()
    m.process([1] * 6000)
    m.set_snapshot(True)
    assert m.state()["music_gain"] == 1.0
    m.set_snapshot(False)


def test_reverb_send_follows_music_gain():
    m = make()
    out = m.process([1] * 6000 + [0] * 6000)
    assert out["send"] == out["music"]


def test_block_processing_matches_whole_buffer():
    flags = [1] * 3000 + [0] * 3000 + [1] * 1000
    whole = make().process(flags)["music"]
    blocked = make()
    first = blocked.process_block(flags[:3500])["music"]
    second = blocked.process_block(flags[3500:])["music"]
    assert first + second == whole


def test_force_release_ends_stuck_duck():
    m = make()
    m.process([1] * 6000)
    m.force_release()
    gains = m.process([0] * 20000)["music"]
    assert abs(gains[-1] - 1.0) < 1e-9


def test_attack_and_release_preserve_seconds_at_any_rate():
    low = make(sample_rate=44100, attack_seconds=0.01, release_seconds=0.2)
    high = make(sample_rate=48000, attack_seconds=0.01, release_seconds=0.2)
    assert abs(low.attack_samples() / 44100 - 0.01) < 1e-6
    assert abs(high.attack_samples() / 48000 - 0.01) < 1e-6
    assert abs(low.release_samples() / 44100 - 0.2) < 1e-6


def test_envelope_is_continuous_across_speech_boundary():
    m = make()
    gains = m.process([1] * 4800 + [0] * 4800)["music"]
    steps = [abs(gains[i] - gains[i - 1]) for i in range(1, len(gains))]
    assert max(steps) < 0.01


def test_duck_cycles_do_not_lose_baseline():
    m = make()
    for _ in range(3):
        m.process([1] * 4800 + [0] * 12000)
    out = m.process([0] * 48000)["music"]
    assert out[-1] == 1.0


def test_gain_stays_between_floor_and_base():
    m = make()
    gains = m.process([1] * 5000 + [0] * 5000)["music"]
    assert all(0.25 <= gain <= 1.0 for gain in gains)


def test_both_channels_share_one_envelope():
    m = make()
    m.process([1] * 6000)
    channels = m.state()["channel_gains"]
    assert channels == (channels[0], channels[0])


def test_release_finishes_within_configured_release_window():
    m = make(release_seconds=0.2)
    m.process([1] * 6000)
    gains = m.process([0] * 12000)["music"]
    release = m.release_samples()
    assert abs(gains[release] - 1.0) < 1e-6
    assert abs(gains[-1] - 1.0) < 1e-9
