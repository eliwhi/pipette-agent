from pipette_agent.deck import Deck, TipRack, plate_96, reservoir
from pipette_agent.session import Session


def make_session():
    deck = Deck(
        [
            reservoir("stock", "stock", 2000, {"x": 100.0}),
            reservoir("buffer", "buffer", 2000),
            plate_96("plate1", "plate"),
        ],
        TipRack("tips", 1000.0, 3),
    )
    return Session(deck)


def outcome(r):
    return r["outcome"], r.get("rule")


def test_needs_tip():
    s = make_session()
    assert outcome(s.call("aspirate", {"labware": "stock", "well": "A1", "volume_ul": 10})) == ("blocked", "no_tip")


def test_volume_range_and_overfill():
    s = make_session()
    s.call("pick_up_tip")
    assert outcome(s.call("aspirate", {"labware": "stock", "well": "A1", "volume_ul": 0.5}))[1] == "volume_out_of_range"
    assert outcome(s.call("aspirate", {"labware": "stock", "well": "A1", "volume_ul": 1200}))[1] == "volume_out_of_range"
    s.call("aspirate", {"labware": "stock", "well": "A1", "volume_ul": 400})
    assert outcome(s.call("dispense", {"labware": "plate1", "well": "A1", "volume_ul": 400}))[1] == "well_overflow"


def test_dead_volume_and_unknown_location():
    s = make_session()
    s.call("pick_up_tip")
    # 2000 uL with 500 uL dead volume: only 1500 aspirable, and tip is 1000 max
    assert outcome(s.call("aspirate", {"labware": "stock", "well": "A1", "volume_ul": 1000}))[0] == "ok"
    s.call("dispense", {"labware": "waste", "well": "A1", "volume_ul": 1000})
    s.call("drop_tip")
    s.call("pick_up_tip")
    assert outcome(s.call("aspirate", {"labware": "stock", "well": "A1", "volume_ul": 600}))[1] == "insufficient_volume"
    assert outcome(s.call("aspirate", {"labware": "plate9", "well": "A1", "volume_ul": 10}))[1] == "unknown_location"


def test_source_contamination():
    s = make_session()
    s.call("pick_up_tip")
    s.call("aspirate", {"labware": "buffer", "well": "A1", "volume_ul": 50})
    s.call("dispense", {"labware": "plate1", "well": "A1", "volume_ul": 50})
    assert outcome(s.call("aspirate", {"labware": "buffer", "well": "A1", "volume_ul": 50}))[1] == "source_contamination"
    assert outcome(s.call("aspirate", {"labware": "stock", "well": "A1", "volume_ul": 50}))[1] == "source_contamination"


def test_blocked_action_changes_nothing():
    s = make_session()
    s.call("pick_up_tip")
    s.call("aspirate", {"labware": "stock", "well": "A1", "volume_ul": 400})
    before = s.deck.get("plate1").well("A1").volume
    s.call("dispense", {"labware": "plate1", "well": "A1", "volume_ul": 400})
    assert s.deck.get("plate1").well("A1").volume == before
    assert s.handler.tip.volume == 400


def test_mixing_concentration():
    s = make_session()
    s.call("transfer", {"source_labware": "buffer", "source_well": "A1", "dest_labware": "plate1", "dest_well": "A1", "volume_ul": 75})
    s.call("transfer", {"source_labware": "stock", "source_well": "A1", "dest_labware": "plate1", "dest_well": "A1", "volume_ul": 25})
    w = s.deck.get("plate1").well("A1")
    assert abs(w.volume - 100) < 1e-9
    assert abs(w.concentration("x") - 25.0) < 1e-9
