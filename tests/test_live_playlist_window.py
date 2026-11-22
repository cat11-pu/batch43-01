import pytest

import live_playlist_window as lpw


def make(**kwargs):
    return lpw.LivePlaylistWindow(**kwargs)


def appended(p, segment="s1", data=b"segment-1", **kwargs):
    return p.append(segment, data, **kwargs)


def test_duplicate_upload_returns_the_same_uri():
    p = make()
    first = appended(p, "s1", b"same-bytes")
    second = p.append("s1", b"same-bytes")
    assert second["status"] == "duplicate"
    assert second["uri"] == first["uri"]


def test_different_bytes_for_same_segment_are_rejected():
    p = make()
    appended(p, "s1", b"first")
    with pytest.raises(lpw.PlaylistError) as exc:
        p.append("s1", b"second")
    assert exc.value.code == "segment_conflict"


def test_publish_makes_segment_visible_immediately():
    p = make()
    appended(p, "s1")
    p.publish("s1")
    rows = p.playlist()
    assert [row["segment_id"] for row in rows] == ["s1"]
    assert rows[0]["duration"] == pytest.approx(2.0)


def test_published_duration_is_immutable():
    p = make()
    appended(p, "s1", duration=2.0)
    p.publish("s1")
    with pytest.raises(lpw.PlaylistError) as exc:
        p.retime("s1", 3.0)
    assert exc.value.code == "immutable_segment"
    assert p.playlist()[0]["duration"] == pytest.approx(2.0)


def test_published_sequence_is_immutable():
    p = make()
    appended(p, "s1")
    p.publish("s1")
    original = p.playlist()[0]["sequence"]
    with pytest.raises(lpw.PlaylistError) as exc:
        p.resequence("s1", 99)
    assert exc.value.code == "immutable_segment"
    assert p.playlist()[0]["sequence"] == original


def test_sequence_cannot_roll_back():
    p = make()
    p.append("s1", b"one", sequence=5)
    with pytest.raises(lpw.PlaylistError) as exc:
        p.append("s2", b"two", sequence=4)
    assert exc.value.code == "sequence_rolled_back"
    out = p.append("s2", b"two", sequence=6)
    assert out["sequence"] == 6


def test_rotate_drops_oldest_and_never_resurrects_it():
    p = make(window=2)
    for index in range(3):
        segment = "s%d" % index
        p.append(segment, ("data-%d" % index).encode())
        p.publish(segment)
    assert [row["segment_id"] for row in p.playlist()] == ["s1", "s2"]
    p.rotate()
    assert [row["segment_id"] for row in p.playlist()] == ["s1", "s2"]
    assert p.publish("s0")["status"] == "already_published"
    assert [row["segment_id"] for row in p.playlist()] == ["s1", "s2"]


def test_recover_continues_media_sequence_from_watermark():
    p = make()
    for index in range(1, 4):
        segment = "s%d" % index
        p.append(segment, ("data-%d" % index).encode())
        p.publish(segment)
    state = p.snapshot()
    q = make()
    q.recover(state)
    out = q.append("s4", b"data-4")
    assert out["sequence"] == 4


def test_recover_keeps_duplicate_detection():
    p = make()
    first = appended(p, "s1", b"payload")
    state = p.snapshot()
    q = make()
    q.recover(state)
    again = q.append("s1", b"payload")
    assert again["status"] == "duplicate"
    assert again["uri"] == first["uri"]


def test_incomplete_segment_cannot_be_published():
    p = make()
    p.append("s1", b"partial", complete=False)
    with pytest.raises(lpw.PlaylistError) as exc:
        p.publish("s1")
    assert exc.value.code == "segment_incomplete"
    assert p.playlist() == []


def test_blocking_refresh_returns_new_playlist():
    p = make()
    appended(p, "s1")
    p.publish("s1")
    out = p.blocking_refresh(since_sequence=0, now=1)
    assert out["status"] == 200
    assert [row["segment_id"] for row in out["playlist"]] == ["s1"]


def test_blocking_refresh_timeout_returns_504():
    p = make()
    out = p.blocking_refresh(since_sequence=5, now=1, wait_seconds=0.5)
    assert out["status"] == 504


def test_part_boundary_outside_segment_is_rejected():
    p = make()
    appended(p, "s1", duration=2.0)
    p.publish("s1")
    with pytest.raises(lpw.PlaylistError) as exc:
        p.add_part("s1", 1.5, 2.5)
    assert exc.value.code == "part_out_of_range"


def test_part_duration_must_match_the_target():
    p = make(part_target=0.5)
    appended(p, "s1", duration=2.0)
    p.publish("s1")
    good = p.add_part("s1", 0.0, 0.5)
    assert good["status"] == "part_added"
    with pytest.raises(lpw.PlaylistError) as exc:
        p.add_part("s1", 0.5, 1.2)
    assert exc.value.code == "part_duration_mismatch"


def test_multi_rendition_sequences_must_stay_in_sync():
    p = make()
    p.append("s1", b"bytes", sequence=10, stream="720p")
    with pytest.raises(lpw.PlaylistError) as exc:
        p.append("s1", b"bytes", sequence=11, stream="1080p")
    assert exc.value.code == "sequence_mismatch"
