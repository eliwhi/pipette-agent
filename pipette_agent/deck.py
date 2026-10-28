"""Deck model: labware, wells and the liquid inside them.

All volumes are in microlitres (uL). Each well tracks its total volume and the
amount of each solute it contains (amount = concentration x volume), so mixing
two liquids gives the correct resulting concentration.
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field


@dataclass
class Well:
    name: str
    max_volume: float
    volume: float = 0.0
    amounts: dict[str, float] = field(default_factory=dict)  # solute -> conc*uL

    def concentration(self, solute: str) -> float:
        if self.volume <= 0:
            return 0.0
        return self.amounts.get(solute, 0.0) / self.volume

    def concentrations(self) -> dict[str, float]:
        return {s: self.concentration(s) for s in self.amounts if self.amounts[s] > 0}

    def remove(self, volume: float) -> dict[str, float]:
        """Take `volume` uL out; return the solute amounts removed."""
        frac = volume / self.volume if self.volume > 0 else 0.0
        taken = {s: a * frac for s, a in self.amounts.items()}
        for s in taken:
            self.amounts[s] -= taken[s]
        self.volume -= volume
        return taken

    def add(self, volume: float, amounts: dict[str, float]) -> None:
        self.volume += volume
        for s, a in amounts.items():
            self.amounts[s] = self.amounts.get(s, 0.0) + a


@dataclass
class Labware:
    """A plate or reservoir on the deck."""

    id: str
    kind: str  # "plate_96", "reservoir", "tube_rack"
    description: str
    wells: dict[str, Well]
    is_source: bool = False  # shared source liquid (stock, buffer): protect from contamination
    dead_volume: float = 0.0  # liquid that can't be aspirated from each well

    def well(self, name: str) -> Well | None:
        return self.wells.get(name.upper())


def plate_96(labware_id: str, description: str, max_volume: float = 300.0) -> Labware:
    wells = {
        f"{row}{col}": Well(f"{row}{col}", max_volume)
        for row in string.ascii_uppercase[:8]
        for col in range(1, 13)
    }
    return Labware(labware_id, "plate_96", description, wells)


def reservoir(
    labware_id: str,
    description: str,
    volume: float,
    solutes: dict[str, float] | None = None,
    max_volume: float = 25_000.0,
    dead_volume: float = 500.0,
) -> Labware:
    """Single-trough reservoir holding `volume` uL with `solutes` (name -> concentration)."""
    w = Well("A1", max_volume)
    w.add(volume, {s: c * volume for s, c in (solutes or {}).items()})
    return Labware(labware_id, "reservoir", description, {"A1": w}, is_source=True, dead_volume=dead_volume)


@dataclass
class TipRack:
    id: str
    tip_capacity: float = 1000.0
    remaining: int = 96


@dataclass
class Tip:
    capacity: float
    volume: float = 0.0
    amounts: dict[str, float] = field(default_factory=dict)
    touched: set[str] = field(default_factory=set)  # labware ids this tip has aspirated from or dispensed into

    @property
    def used(self) -> bool:
        return bool(self.touched)


class Deck:
    def __init__(self, labware: list[Labware], tip_rack: TipRack, waste_id: str = "waste"):
        self.labware: dict[str, Labware] = {lw.id: lw for lw in labware}
        self.tip_rack = tip_rack
        self.waste_id = waste_id
        self.waste_volume = 0.0

    def get(self, labware_id: str) -> Labware | None:
        return self.labware.get(labware_id)

    def snapshot(self, include_empty: bool = False) -> dict:
        """Compact JSON-friendly view of the deck for the agent."""
        out = {}
        for lw in self.labware.values():
            wells = {}
            for name, w in lw.wells.items():
                if w.volume > 1e-9 or include_empty:
                    wells[name] = {
                        "volume_ul": round(w.volume, 3),
                        "concentrations": {s: round(c, 4) for s, c in w.concentrations().items()},
                    }
            out[lw.id] = {
                "kind": lw.kind,
                "description": lw.description,
                "max_volume_per_well_ul": next(iter(lw.wells.values())).max_volume,
                "is_shared_source": lw.is_source,
                "dead_volume_ul": lw.dead_volume,
                "non_empty_wells": wells,
            }
        return out
