"""Safety guard: every action is checked in code before the hardware moves.

The model never gets to decide whether an action is safe. If a rule is broken
the action is blocked, logged, and a structured explanation is returned to the
agent so it can correct course.
"""

from __future__ import annotations

from dataclasses import dataclass

from .deck import Deck
from .handler import LiquidHandler

EPS = 1e-6


@dataclass
class Violation:
    rule: str
    message: str

    def as_dict(self) -> dict:
        return {"blocked": True, "rule": self.rule, "message": self.message}


RULES = {
    "unknown_location": "Labware or well does not exist on the deck.",
    "no_tip": "Action needs a tip on the channel.",
    "tip_already_loaded": "A tip is already loaded; drop it first.",
    "no_tips_left": "The tip rack is empty.",
    "volume_out_of_range": "Volume is outside the pipette's working range.",
    "tip_overfill": "Aspiration would exceed the tip's capacity.",
    "insufficient_volume": "Not enough aspirable liquid in the source well (dead volume included).",
    "dispense_exceeds_tip": "Dispense volume is more than the tip holds.",
    "well_overflow": "Dispense would overflow the destination well.",
    "source_contamination": "A used tip may not enter a shared source (stock or buffer reservoir).",
    "aspirate_from_waste": "Cannot aspirate from waste.",
}


class SafetyGuard:
    def __init__(self, deck: Deck, handler: LiquidHandler):
        self.deck = deck
        self.handler = handler

    def _location(self, labware_id: str, well: str, allow_waste: bool = False) -> Violation | None:
        if allow_waste and labware_id == self.deck.waste_id:
            return None
        lw = self.deck.get(labware_id)
        if lw is None or lw.well(well) is None:
            return Violation("unknown_location", f"{labware_id}:{well} is not on the deck.")
        return None

    def check(self, action: str, args: dict) -> Violation | None:
        h = self.handler
        if action == "pick_up_tip":
            if h.tip is not None:
                return Violation("tip_already_loaded", "A tip is already loaded. Call drop_tip first.")
            if self.deck.tip_rack.remaining <= 0:
                return Violation("no_tips_left", "No tips remaining in the rack.")
            return None

        if action == "drop_tip":
            if h.tip is None:
                return Violation("no_tip", "There is no tip to drop.")
            return None

        if action in ("aspirate", "dispense"):
            labware_id, well, vol = args["labware"], args["well"], float(args["volume_ul"])
            if h.tip is None:
                return Violation("no_tip", f"Cannot {action} without a tip. Call pick_up_tip first.")
            if vol < h.MIN_VOLUME_UL - EPS or vol > h.tip.capacity + EPS:
                return Violation(
                    "volume_out_of_range",
                    f"{vol} uL is outside the working range {h.MIN_VOLUME_UL}-{h.tip.capacity} uL.",
                )

            if action == "aspirate":
                if labware_id == self.deck.waste_id:
                    return Violation("aspirate_from_waste", "Aspirating from waste is not allowed.")
                if v := self._location(labware_id, well):
                    return v
                lw = self.deck.get(labware_id)
                w = lw.well(well)
                if h.tip.volume + vol > h.tip.capacity + EPS:
                    return Violation(
                        "tip_overfill",
                        f"Tip holds {h.tip.volume:.2f} uL; adding {vol} uL exceeds {h.tip.capacity} uL.",
                    )
                if vol > w.volume - lw.dead_volume + EPS:
                    return Violation(
                        "insufficient_volume",
                        f"{labware_id}:{well} has {w.volume:.2f} uL with {lw.dead_volume} uL dead volume; "
                        f"cannot aspirate {vol} uL.",
                    )
                if lw.is_source and (h.tip.touched - {labware_id}):
                    return Violation(
                        "source_contamination",
                        f"This tip has already touched {sorted(h.tip.touched)}. "
                        f"Use a fresh tip to enter the shared source {labware_id}.",
                    )
                return None

            # dispense
            if v := self._location(labware_id, well, allow_waste=True):
                return v
            if vol > h.tip.volume + EPS:
                return Violation(
                    "dispense_exceeds_tip", f"Tip holds {h.tip.volume:.2f} uL; cannot dispense {vol} uL."
                )
            if labware_id != self.deck.waste_id:
                w = self.deck.get(labware_id).well(well)
                if w.volume + vol > w.max_volume + EPS:
                    return Violation(
                        "well_overflow",
                        f"{labware_id}:{well} holds {w.volume:.2f} uL (max {w.max_volume} uL); "
                        f"adding {vol} uL would overflow.",
                    )
                if self.deck.get(labware_id).is_source:
                    return Violation(
                        "source_contamination", f"Dispensing back into the shared source {labware_id} is not allowed."
                    )
            return None

        return None
