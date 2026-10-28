"""Simulated single-channel liquid handler.

The handler executes actions *after* the safety guard has approved them. It
models the physical side only: tips, aspirate/dispense, waste. It can also
inject hardware faults (failed tip pickup, liquid shortfall detected by
liquid-level detection) so evals can test how an agent recovers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .deck import Deck, Tip


class HardwareError(Exception):
    """A physical failure the agent could not have predicted from the deck state."""


@dataclass
class Faults:
    tip_pickup_fail_on: list[int] = field(default_factory=list)  # 1-based pickup attempt numbers that fail
    # labware_id -> true starting volume (uL), lower than what the deck reports
    liquid_shortfall: dict[str, float] = field(default_factory=dict)


class LiquidHandler:
    MIN_VOLUME_UL = 1.0

    def __init__(self, deck: Deck, faults: Faults | None = None):
        self.deck = deck
        self.faults = faults or Faults()
        self.tip: Tip | None = None
        self.pickup_attempts = 0
        self.tips_used = 0
        # true volumes for labware with an injected shortfall
        self._true_volume = dict(self.faults.liquid_shortfall)

    # --- actions -----------------------------------------------------------
    def pick_up_tip(self) -> dict:
        self.pickup_attempts += 1
        self.deck.tip_rack.remaining -= 1  # a tip is consumed even if pickup fails
        if self.pickup_attempts in self.faults.tip_pickup_fail_on:
            raise HardwareError("Tip pickup failed: no tip detected on channel. The tip position was skipped.")
        self.tip = Tip(capacity=self.deck.tip_rack.tip_capacity)
        self.tips_used += 1
        return {"tip_capacity_ul": self.tip.capacity, "tips_remaining": self.deck.tip_rack.remaining}

    def drop_tip(self) -> dict:
        if self.tip and self.tip.volume > 0:
            self.deck.waste_volume += self.tip.volume
        self.tip = None
        return {"dropped": True}

    def aspirate(self, labware_id: str, well: str, volume: float) -> dict:
        lw = self.deck.get(labware_id)
        w = lw.well(well)
        if labware_id in self._true_volume:
            available = self._true_volume[labware_id] - lw.dead_volume
            if volume > available + 1e-9:
                raise HardwareError(
                    f"Liquid level detection: insufficient liquid in {labware_id}:{well}. "
                    f"Aspiration of {volume} uL aborted; the trough appears to hold far less than recorded."
                )
            self._true_volume[labware_id] -= volume
        taken = w.remove(volume)
        self.tip.volume += volume
        for s, a in taken.items():
            self.tip.amounts[s] = self.tip.amounts.get(s, 0.0) + a
        self.tip.touched.add(labware_id)
        return {"aspirated_ul": volume, "tip_volume_ul": round(self.tip.volume, 3)}

    def dispense(self, labware_id: str, well: str, volume: float) -> dict:
        frac = volume / self.tip.volume
        given = {s: a * frac for s, a in self.tip.amounts.items()}
        for s in given:
            self.tip.amounts[s] -= given[s]
        self.tip.volume -= volume
        if self.tip.volume < 1e-9:
            self.tip.volume = 0.0
            self.tip.amounts = {}
        if labware_id == self.deck.waste_id:
            self.deck.waste_volume += volume
        else:
            self.deck.get(labware_id).well(well).add(volume, given)
            self.tip.touched.add(labware_id)
        return {"dispensed_ul": volume, "tip_volume_ul": round(self.tip.volume, 6)}
