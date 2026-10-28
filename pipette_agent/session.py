"""A run session: deck + handler + guard, exposed as tools with a full audit log."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .deck import Deck
from .guard import RULES, SafetyGuard
from .handler import Faults, HardwareError, LiquidHandler, LiquidLevelError

TOOL_SCHEMAS = [
    {
        "name": "get_deck_state",
        "description": (
            "Return the current deck: every labware item, its non-empty wells with volumes (uL) and "
            "solute concentrations, the tip status and tips remaining. Call this before planning."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "pick_up_tip",
        "description": "Pick up a fresh 1000 uL tip on the single channel.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "drop_tip",
        "description": "Eject the current tip (and any liquid in it) to waste.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "aspirate",
        "description": "Aspirate volume_ul from a well into the current tip.",
        "input_schema": {
            "type": "object",
            "properties": {
                "labware": {"type": "string", "description": "Labware id, e.g. 'plate1' or 'buffer'"},
                "well": {"type": "string", "description": "Well name, e.g. 'A1'. Reservoirs have a single well 'A1'."},
                "volume_ul": {"type": "number"},
            },
            "required": ["labware", "well", "volume_ul"],
        },
    },
    {
        "name": "dispense",
        "description": "Dispense volume_ul from the current tip into a well. Use labware 'waste' to discard liquid.",
        "input_schema": {
            "type": "object",
            "properties": {
                "labware": {"type": "string"},
                "well": {"type": "string"},
                "volume_ul": {"type": "number"},
            },
            "required": ["labware", "well", "volume_ul"],
        },
    },
    {
        "name": "transfer",
        "description": (
            "Convenience action: optionally pick up a fresh tip, aspirate volume_ul from the source, "
            "dispense it into the destination, then drop the tip. Each step passes the same safety checks. "
            "Stops at the first blocked or failed step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_labware": {"type": "string"},
                "source_well": {"type": "string"},
                "dest_labware": {"type": "string"},
                "dest_well": {"type": "string"},
                "volume_ul": {"type": "number"},
                "new_tip": {"type": "boolean", "description": "Use a fresh tip and drop it afterwards (default true)."},
            },
            "required": ["source_labware", "source_well", "dest_labware", "dest_well", "volume_ul"],
        },
    },
    {
        "name": "finish",
        "description": (
            "End the run. status must be 'completed' only if the task was fully achieved; use 'aborted' if it "
            "could not be done safely or correctly, and explain why in summary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["completed", "aborted"]},
                "summary": {"type": "string"},
            },
            "required": ["status", "summary"],
        },
    },
]


@dataclass
class Event:
    step: int
    action: str
    args: dict
    outcome: str  # ok | blocked | hardware_error | invalid
    detail: dict

    def as_dict(self) -> dict:
        return {"step": self.step, "action": self.action, "args": self.args, "outcome": self.outcome, "detail": self.detail}


@dataclass
class Session:
    deck: Deck
    faults: Faults = field(default_factory=Faults)
    events: list[Event] = field(default_factory=list)
    finished: dict | None = None

    def __post_init__(self):
        self.handler = LiquidHandler(self.deck, self.faults)
        self.guard = SafetyGuard(self.deck, self.handler)

    # --- helpers -------------------------------------------------------------
    def _log(self, action, args, outcome, detail) -> dict:
        self.events.append(Event(len(self.events) + 1, action, args, outcome, detail))
        return {"outcome": outcome, **detail}

    def state(self) -> dict:
        h = self.handler
        return {
            "labware": self.deck.snapshot(),
            "waste_labware_id": self.deck.waste_id,
            "tip": None
            if h.tip is None
            else {"loaded": True, "volume_ul": round(h.tip.volume, 3), "capacity_ul": h.tip.capacity, "touched": sorted(h.tip.touched)},
            "tips_remaining": self.deck.tip_rack.remaining,
            "sensor_flagged_labware": sorted(self.guard.sensor_flagged),
            "pipette_range_ul": [h.MIN_VOLUME_UL, self.deck.tip_rack.tip_capacity],
            "safety_rules": RULES,
        }

    def _atomic(self, action: str, args: dict) -> dict:
        violation = self.guard.check(action, args)
        if violation:
            return self._log(action, args, "blocked", violation.as_dict())
        try:
            if action == "pick_up_tip":
                result = self.handler.pick_up_tip()
            elif action == "drop_tip":
                result = self.handler.drop_tip()
            elif action == "aspirate":
                result = self.handler.aspirate(args["labware"], args["well"].upper(), float(args["volume_ul"]))
            elif action == "dispense":
                result = self.handler.dispense(args["labware"], args["well"].upper(), float(args["volume_ul"]))
            else:
                return self._log(action, args, "invalid", {"error": f"Unknown action {action}"})
        except HardwareError as e:
            if isinstance(e, LiquidLevelError):
                self.guard.sensor_flagged.add(e.labware_id)
            return self._log(action, args, "hardware_error", {"error": str(e)})
        return self._log(action, args, "ok", result)

    # --- public entry point -------------------------------------------------------
    def call(self, name: str, args: dict | None = None) -> dict:
        args = dict(args or {})
        if self.finished is not None:
            return {"outcome": "invalid", "error": "Run already finished."}
        if name == "get_deck_state":
            return self.state()
        if name == "finish":
            self.finished = {"status": args.get("status"), "summary": args.get("summary", "")}
            self._log("finish", args, "ok", {})
            return {"outcome": "ok", "finished": True}
        if name == "transfer":
            new_tip = args.get("new_tip", True)
            steps = []
            if new_tip:
                if self.handler.tip is not None:
                    steps.append(("drop_tip", {}))
                steps.append(("pick_up_tip", {}))
            steps.append(("aspirate", {"labware": args["source_labware"], "well": args["source_well"], "volume_ul": args["volume_ul"]}))
            steps.append(("dispense", {"labware": args["dest_labware"], "well": args["dest_well"], "volume_ul": args["volume_ul"]}))
            if new_tip:
                steps.append(("drop_tip", {}))
            results = []
            for action, a in steps:
                r = self._atomic(action, a)
                results.append({"action": action, **r})
                if r["outcome"] != "ok":
                    return {"outcome": r["outcome"], "failed_step": action, "steps": results}
            return {"outcome": "ok", "steps": results}
        if name in ("pick_up_tip", "drop_tip", "aspirate", "dispense"):
            return self._atomic(name, args)
        return self._log(name, args, "invalid", {"error": f"Unknown tool {name}"})

    def call_json(self, name: str, args: dict | None = None) -> str:
        return json.dumps(self.call(name, args))
