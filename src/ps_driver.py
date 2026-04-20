"""Python client for the Node.js PS driver (driver/driver.js).

Spawns the driver as a subprocess and exposes a synchronous request/response API.
Line-delimited JSON-RPC on stdin/stdout.
"""
import json
import os
import subprocess
import threading
import uuid
from typing import Any, Dict, Optional


DRIVER_JS = os.path.join(os.path.dirname(__file__), "..", "driver", "driver.js")


class PSDriver:
    def __init__(self, driver_path: str = DRIVER_JS):
        self.proc = subprocess.Popen(
            ["node", driver_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._stderr_lines = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self):
        for line in iter(self.proc.stderr.readline, ''):
            self._stderr_lines.append(line.rstrip())
            print(f"[driver.js] {line.rstrip()}", flush=True)

    def close(self) -> None:
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        self.proc.wait(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        req_id = str(uuid.uuid4())
        req = {"id": req_id, "method": method, "params": params or {}}
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()

        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"driver closed unexpectedly. stderr:\n{stderr}")

        resp = json.loads(line)
        if resp.get("id") != req_id:
            raise RuntimeError(f"id mismatch: sent {req_id}, got {resp.get('id')}")
        if "error" in resp:
            raise RuntimeError(f"driver error: {resp['error']}")
        return resp.get("result")

    def damage_range(self, attacker: Dict, defender: Dict, move: str) -> Dict:
        return self.call("damage_range", {"attacker": attacker, "defender": defender, "move": move})

    def start_battle(self, p1_team: list, p2_team: list, seed: Optional[list] = None) -> Dict:
        params = {"teams": {"p1": p1_team, "p2": p2_team}}
        if seed is not None:
            params["seed"] = seed
        return self.call("start_battle", params)

    def execute_turn(self, state: Dict, actions: Dict, rng_overrides: Optional[list] = None) -> Dict:
        return self.call("execute_turn", {"state": state, "actions": actions, "rng_overrides": rng_overrides or []})

    def enumerate_turn(self, state: Dict) -> Dict:
        return self.call("enumerate_turn", {"state": state})


if __name__ == "__main__":
    # Smoke test: Terrakion Rock Slide vs Scrafty (from CLAUDE.md team)
    terrakion = {
        "species": "Terrakion", "item": "Wide Lens", "ability": "Justified",
        "moves": ["Rock Slide", "Sacred Sword", "X-Scissor", "Protect"],
        "nature": "Jolly",
        "evs": {"atk": 252, "spd": 4, "spe": 252},
    }
    whimsicott = {
        "species": "Whimsicott", "item": "Focus Sash", "ability": "Prankster",
        "moves": ["Giga Drain", "Beat Up", "Tailwind", "Sunny Day"],
        "nature": "Timid",
        "evs": {"def": 4, "spa": 252, "spe": 252},
    }
    scrafty = {
        "species": "Scrafty", "item": "Leftovers", "ability": "Intimidate",
        "moves": ["Fake Out", "Drain Punch", "Crunch", "Protect"],
        "nature": "Adamant",
        "evs": {"hp": 252, "atk": 4, "spd": 252},
    }
    gothitelle = {
        "species": "Gothitelle", "item": "Leftovers", "ability": "Shadow Tag",
        "moves": ["Psychic", "Protect", "Trick Room", "Thunderbolt"],
        "nature": "Quiet",
        "evs": {"hp": 252, "spa": 252, "spd": 4},
    }

    with PSDriver() as drv:
        res = drv.damage_range(terrakion, scrafty, "Rock Slide")
        print("Terrakion Rock Slide vs Scrafty:")
        print(json.dumps(res, indent=2))

        res2 = drv.damage_range(terrakion, scrafty, "Sacred Sword")
        print("\nTerrakion Sacred Sword vs Scrafty:")
        print(json.dumps(res2, indent=2))

        # Full turn smoke test: Terrakion Sacred Sword → Scrafty, Whimsicott Tailwind.
        # Opponents Protect. Force min and max damage rolls to verify RNG hooks.
        print("\n--- start_battle + execute_turn ---")
        start = drv.start_battle([terrakion, whimsicott], [scrafty, gothitelle])
        print(f"state serialized: {len(json.dumps(start['state']))} bytes")

        actions = {
            "p1": [
                {"type": "move", "move": "Sacred Sword", "target": "foeSlot0"},
                {"type": "move", "move": "Tailwind"},
            ],
            "p2": [
                {"type": "move", "move": "Protect"},
                {"type": "move", "move": "Protect"},
            ],
        }

        # Force max damage (roll 0). Protect will block it, but the hook still fires.
        turn = drv.execute_turn(start["state"], actions, rng_overrides=[
            {"kind": "damage_roll", "actor": "p1.slot0", "force": "max"},
        ])
        print(f"choices: {turn['choices']}")
        print(f"rng_events: {json.dumps(turn['rng_events'], indent=2)}")

        # Execute a second turn on the returned state to confirm round-trip.
        actions2 = {
            "p1": [
                {"type": "move", "move": "Sacred Sword", "target": "foeSlot0"},
                {"type": "move", "move": "Giga Drain", "target": "foeSlot1"},
            ],
            "p2": [
                {"type": "move", "move": "Crunch", "target": "foeSlot0"},
                {"type": "move", "move": "Psychic", "target": "foeSlot0"},
            ],
        }
        turn2 = drv.execute_turn(turn["state"], actions2, rng_overrides=[
            {"kind": "damage_roll", "force": "min"},
            {"kind": "accuracy", "actor": "p1.slot0", "force": "miss"},  # Terrakion Sacred Sword misses
            {"kind": "crit", "actor": "p2.slot0", "force": "crit"},       # Scrafty Crunch crits
            {"kind": "secondary", "actor": "p2.slot1", "move": "Psychic", "force": "proc"},
        ])
        print(f"\nturn 2 choices: {turn2['choices']}")
        print(f"turn 2 rng_events: {json.dumps(turn2['rng_events'], indent=2)}")

        # enumerate_turn on the post-turn-2 state.
        print("\n--- enumerate_turn ---")
        enum = drv.enumerate_turn(turn2["state"])
        print(f"p1 side choices: {enum['p1_choice_count']}")
        print(f"p2 side choices: {enum['p2_choice_count']}")
        print(f"total action pairs: {len(enum['action_pairs'])}")
        print("\nfirst 5 p1 side choices:")
        seen = set()
        for pair in enum["action_pairs"]:
            key = json.dumps(pair["p1"])
            if key in seen: continue
            seen.add(key)
            print(f"  {pair['p1']}")
            if len(seen) >= 5: break

        # --- switch-phase round-trip ---
        # Fresh battle, 4-mon p2 so there's a bench to switch from.
        # Forced crit + max damage roll means Terrakion's Sacred Sword OHKOs
        # Scrafty (base range 92-110 → ~184-220 on crit, vs 172 HP).
        print("\n--- switch-phase enumeration ---")
        darmanitan = {
            "species": "Darmanitan", "item": "Life Orb", "ability": "Sheer Force",
            "moves": ["Flare Blitz", "Rock Slide", "Earthquake", "Protect"],
            "nature": "Adamant",
            "evs": {"atk": 252, "spd": 4, "spe": 252},
        }
        thundurus = {
            "species": "Thundurus", "item": "Expert Belt", "ability": "Prankster",
            "moves": ["Thunderbolt", "Grass Knot", "Psychic", "Dark Pulse"],
            "nature": "Timid",
            "ivs": {"atk": 0},
            "evs": {"def": 4, "spa": 252, "spe": 252},
        }

        sw_start = drv.start_battle(
            [terrakion, whimsicott],
            [scrafty, gothitelle, darmanitan, thundurus],
        )
        sw_turn = drv.execute_turn(sw_start["state"], {
            "p1": [
                {"type": "move", "move": "Sacred Sword", "target": "foeSlot0"},
                {"type": "move", "move": "Tailwind"},
            ],
            "p2": [
                {"type": "move", "move": "Crunch", "target": "foeSlot0"},
                {"type": "move", "move": "Protect"},
            ],
        }, rng_overrides=[
            {"kind": "damage_roll", "force": "max"},
            {"kind": "crit", "actor": "p1.slot0", "force": "crit"},
        ])

        sw_enum = drv.enumerate_turn(sw_turn["state"])
        print(f"requestState: {sw_enum.get('requestState')}")
        print(f"p1 choices: {sw_enum['p1_choice_count']}, p2 choices: {sw_enum['p2_choice_count']}")
        print(f"pairs: {len(sw_enum['action_pairs'])}")
        for i, pair in enumerate(sw_enum["action_pairs"]):
            print(f"  [{i}] p1={pair['p1']} p2={pair['p2']}")

        # Round-trip: commit the first enumerated pair and confirm we're back
        # in a regular 'move' request on the next call.
        first_pair = sw_enum["action_pairs"][0]
        sw_next = drv.execute_turn(sw_turn["state"], {"p1": first_pair["p1"], "p2": first_pair["p2"]})
        after = drv.enumerate_turn(sw_next["state"])
        print(f"\nafter switch committed: requestState={after.get('requestState')}, "
              f"p1_choices={after['p1_choice_count']}, p2_choices={after['p2_choice_count']}")
