"""Python client for the Node.js PS driver (driver/driver.js).

Spawns the driver as a subprocess and exposes a synchronous request/response API.
Line-delimited JSON-RPC on stdin/stdout.
"""
import json
import os
import subprocess
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

    def execute_turn(self, state: Dict, actions: Dict, rng_overrides: Optional[list] = None) -> Dict:
        return self.call("execute_turn", {"state": state, "actions": actions, "rng_overrides": rng_overrides or []})

    def enumerate_turn(self, state: Dict) -> Any:
        return self.call("enumerate_turn", {"state": state})


if __name__ == "__main__":
    # Smoke test: Terrakion Rock Slide vs Scrafty (from CLAUDE.md team)
    terrakion = {
        "species": "Terrakion", "item": "Wide Lens", "ability": "Justified",
        "moves": ["Rock Slide", "Sacred Sword", "X-Scissor", "Protect"],
        "nature": "Jolly",
        "evs": {"atk": 252, "spd": 4, "spe": 252},
    }
    scrafty = {
        "species": "Scrafty", "item": "Leftovers", "ability": "Intimidate",
        "moves": ["Fake Out", "Drain Punch", "Crunch", "Protect"],
        "nature": "Adamant",
        "evs": {"hp": 252, "atk": 4, "spd": 252},
    }

    with PSDriver() as drv:
        res = drv.damage_range(terrakion, scrafty, "Rock Slide")
        print("Terrakion Rock Slide vs Scrafty:")
        print(json.dumps(res, indent=2))

        res2 = drv.damage_range(terrakion, scrafty, "Sacred Sword")
        print("\nTerrakion Sacred Sword vs Scrafty:")
        print(json.dumps(res2, indent=2))

        stub = drv.execute_turn({}, {})
        print(f"\nexecute_turn stub: {stub}")
