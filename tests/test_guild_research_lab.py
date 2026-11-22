import pytest

import guild_research_lab as grl


def make(**kwargs):
    return grl.GuildResearchLab(**kwargs)


def lab(required=100, prereq=None, min_level=1, duration=100, materials=0,
        max_level=5, level=5):
    g = make()
    g.set_level("g1", level)
    g.set_requirements("tech", required, prereq=prereq,
                       min_guild_level=min_level, duration=duration,
                       materials=materials, max_level=max_level)
    return g


def start(g, tech="tech", now=0):
    g.donate("g1", tech, 100, txn_id="seed")
    return g.start_research("g1", tech, now=now)


def test_donation_over_requirement_returns_excess():
    g = lab(required=100)
    out = g.donate("g1", "tech", 150, txn_id="t1")
    assert out["accepted"] == 100
    assert out["returned"] == 50
    assert out["total"] == 100


def test_second_research_in_same_guild_is_refused():
    g = lab()
    g.set_requirements("tech2", 10)
    start(g)
    g.donate("g1", "tech2", 10)
    with pytest.raises(grl.ResearchError) as exc:
        g.start_research("g1", "tech2", now=0)
    assert exc.value.code == "SLOT_BUSY"


def test_research_cannot_finish_after_guild_disband():
    g = lab()
    start(g)
    g.disband("g1")
    with pytest.raises(grl.ResearchError) as exc:
        g.finish_research("g1", "tech", now=100)
    assert exc.value.code == "GUILD_DISSOLVED"
    assert g.tech_level("g1", "tech") == 0


def test_finish_before_server_completion_time_is_refused():
    g = lab(duration=100)
    start(g)
    with pytest.raises(grl.ResearchError) as exc:
        g.finish_research("g1", "tech", now=99)
    assert exc.value.code == "NOT_COMPLETE"
    out = g.finish_research("g1", "tech", now=100)
    assert out["granted"] is True


def test_cancel_refunds_materials_by_progress():
    g = lab(duration=100, materials=40)
    start(g)
    out = g.cancel_research("g1", "tech", now=50)
    assert out["refunded"] == 20
    assert g.research_slot("g1")["tech_id"] is None


def test_duplicate_donation_txn_is_counted_once():
    g = lab(required=100)
    g.donate("g1", "tech", 30, txn_id="same")
    again = g.donate("g1", "tech", 30, txn_id="same")
    assert again["accepted"] == 0
    assert again["duplicate"] is True
    assert g.research_slot("g1")["tech_id"] is None
    with pytest.raises(grl.ResearchError) as exc:
        g.start_research("g1", "tech", now=0)
    assert exc.value.code == "NOT_ENOUGH_POINTS"


def test_prerequisite_tech_is_required():
    g = lab(prereq="base")
    g.set_requirements("base", 10)
    with pytest.raises(grl.ResearchError) as exc:
        start(g)
    assert exc.value.code == "PREREQ_MISSING"


def test_completion_grants_exactly_one_level():
    g = lab()
    start(g)
    g.finish_research("g1", "tech", now=100)
    assert g.tech_level("g1", "tech") == 1
    with pytest.raises(grl.ResearchError):
        g.finish_research("g1", "tech", now=200)
    assert g.tech_level("g1", "tech") == 1


def test_guild_level_drop_blocks_completion():
    g = lab(min_level=5)
    start(g)
    g.set_level("g1", 1)
    with pytest.raises(grl.ResearchError) as exc:
        g.finish_research("g1", "tech", now=100)
    assert exc.value.code == "GUILD_LEVEL_TOO_LOW"
    assert g.tech_level("g1", "tech") == 0


def test_restart_restores_running_research():
    g = lab(duration=100)
    start(g)
    state = g.dump()
    restored = make()
    restored.load(state)
    out = restored.finish_research("g1", "tech", now=100)
    assert out["granted"] is True
    assert restored.tech_level("g1", "tech") == 1


def test_maxed_tech_refuses_more_donation():
    g = lab(max_level=1)
    start(g)
    g.finish_research("g1", "tech", now=100)
    with pytest.raises(grl.ResearchError) as exc:
        g.donate("g1", "tech", 10, txn_id="again")
    assert exc.value.code == "TECH_MAXED"


def test_research_slot_is_idle_after_completion():
    g = lab()
    start(g)
    g.finish_research("g1", "tech", now=100)
    slot = g.research_slot("g1")
    assert slot["tech_id"] is None
    assert slot["ends_at"] is None


def test_donation_total_never_exceeds_requirement():
    g = lab(required=100)
    g.donate("g1", "tech", 60, txn_id="a")
    g.donate("g1", "tech", 60, txn_id="b")
    out = g.donate("g1", "tech", 60, txn_id="c")
    assert out["total"] == 100
    assert out["returned"] == 60


def test_only_one_research_slot_per_guild():
    g = lab()
    g.set_requirements("tech2", 10)
    start(g)
    g.donate("g1", "tech2", 10)
    with pytest.raises(grl.ResearchError):
        g.start_research("g1", "tech2", now=0)


def test_repeated_finish_does_not_grant_twice():
    g = lab()
    start(g)
    first = g.finish_research("g1", "tech", now=100)
    assert first["granted"] is True
    with pytest.raises(grl.ResearchError):
        g.finish_research("g1", "tech", now=101)
    assert g.tech_level("g1", "tech") == 1
