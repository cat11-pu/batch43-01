import pytest

import abr_variant_ladder as avl

VARIANTS = [
    {"id": "360p", "bitrate": 800000, "codec": "avc1", "audio": "aac-96",
     "init": "init-360.mp4"},
    {"id": "720p", "bitrate": 2500000, "codec": "avc1", "audio": "aac-128",
     "init": "init-720.mp4"},
    {"id": "av1", "bitrate": 4000000, "codec": "av01", "audio": "aac-128",
     "init": "init-av1.mp4"},
]


def make(mode="live"):
    ladder = avl.AbrVariantLadder(mode=mode)
    ladder.load_manifest(VARIANTS)
    return ladder


def segment(ladder, seq, keyframes=None, discontinuity=0):
    keyframes = keyframes or {}
    info = {"discontinuity": discontinuity,
            "variants": {vid: {"keyframe": keyframes.get(vid, True)}
                         for vid in ("360p", "720p", "av1")}}
    return ladder.add_segment(seq, info)


def test_single_bad_segment_does_not_drop_to_lowest():
    ladder = make()
    for seq in range(1, 5):
        ladder.on_segment_end(seq, bandwidth_bps=10000000)
    ladder.on_segment_end(5, bandwidth_bps=500000)
    target = ladder.decide(video_buffer=20.0, audio_buffer=20.0)["target"]
    assert target != "360p"


def test_switch_is_deferred_until_segment_boundary():
    ladder = make()
    ladder.on_segment_end(1, bandwidth_bps=5000000)
    segment(ladder, 2)
    out = ladder.request_switch("720p", seq=1)
    assert out["deferred"] is True
    assert ladder.current() == "360p"
    ladder.on_segment_end(2, bandwidth_bps=5000000)
    assert ladder.current() == "720p"


def test_audio_variant_is_switched_with_video():
    ladder = make()
    decision = ladder.decide(video_buffer=20.0, audio_buffer=20.0,
                             bandwidth_bps=10000000)
    assert decision["target"] == "av1"
    assert decision["audio"] == "aac-128"


def test_missing_keyframe_defers_switch():
    ladder = make()
    ladder.on_segment_end(1, bandwidth_bps=5000000)
    segment(ladder, 2, keyframes={"720p": False})
    out = ladder.request_switch("720p", seq=1)
    assert out["switched"] is False
    assert out["reason"] == "NO_KEYFRAME"


def test_discontinuity_must_be_declared():
    ladder = make()
    ladder.on_segment_end(4, bandwidth_bps=1000000)
    with pytest.raises(avl.AbrError) as exc:
        ladder.on_segment_end(3, bandwidth_bps=1000000)
    assert exc.value.code == "CONTINUITY_ERROR"
    segment(ladder, 1, discontinuity=1)
    assert ladder.on_segment_end(1, bandwidth_bps=1000000)["seq"] == 1


def test_seek_resets_controller_without_changing_variant():
    ladder = make()
    ladder.on_segment_end(4, bandwidth_bps=9000000)
    before = ladder.current()
    out = ladder.seek(50)
    assert out["seq"] == 50
    assert ladder.current() == before


def test_audio_buffer_underrun_forces_downswitch():
    ladder = make()
    out = ladder.decide(video_buffer=20.0, audio_buffer=0.5,
                        bandwidth_bps=10000000)
    assert out["target"] == "360p"


def test_live_and_vod_controllers_are_independent():
    live = make(mode="live")
    vod = make(mode="vod")
    live.on_segment_end(1, bandwidth_bps=500000)
    assert live.current() != vod.current() or live.state()["last_seq"] != 0
    assert vod.state()["last_seq"] == 0


def test_removed_variant_falls_back_to_available():
    ladder = make()
    ladder.request_switch("av1", seq=0)
    ladder.load_manifest([VARIANTS[0]])
    assert ladder.current() == "360p"


def test_codec_switch_reports_required_init_segment():
    ladder = make()
    out = ladder.request_switch("av1", seq=0)
    assert out["init_required"] is True
    assert out["init"] == "init-av1.mp4"


def test_upgrade_is_conservative_after_recovery():
    ladder = make()
    ladder.on_segment_end(1, bandwidth_bps=500000, stall=True)
    ladder.on_segment_end(2, bandwidth_bps=10000000)
    assert ladder.current() == "360p"
    ladder.on_segment_end(3, bandwidth_bps=10000000)
    ladder.on_segment_end(4, bandwidth_bps=10000000)
    decision = ladder.decide(video_buffer=20.0, audio_buffer=20.0,
                             bandwidth_bps=10000000)
    assert decision["target"] != "360p"


def test_stall_counter_resets_after_switch():
    ladder = make()
    ladder.on_segment_end(1, bandwidth_bps=500000, stall=True)
    assert ladder.state()["stalls"] == 0


def test_audio_and_video_always_share_one_variant_group():
    ladder = make()
    decision = ladder.decide(video_buffer=20.0, audio_buffer=20.0,
                             bandwidth_bps=3000000)
    pairs = {variant["id"]: variant["audio"] for variant in VARIANTS}
    assert decision["audio"] == pairs[decision["target"]]


def test_failed_switch_keeps_previous_variant():
    ladder = make()
    ladder.on_segment_end(1, bandwidth_bps=5000000)
    segment(ladder, 2, keyframes={"av1": False})
    before = ladder.current()
    out = ladder.request_switch("av1", seq=1)
    assert out["switched"] is False
    assert ladder.current() == before


def test_no_switch_is_applied_mid_segment():
    ladder = make()
    ladder.on_segment_end(1, bandwidth_bps=5000000)
    before = ladder.current()
    ladder.request_switch("720p", seq=1)
    assert ladder.current() == before
