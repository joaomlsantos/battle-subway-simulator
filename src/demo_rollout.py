"""Linear battle rollout demo.

Runs a single battle to completion between two fixed 4-mon teams. At each
step: enumerate legal actions, pick p1's action via adversarial single-ply
minimax (worst-case over p2's choices, using the HP-fraction evaluator),
execute the chosen pair. No RNG overrides — rolls happen naturally.
"""
import json
import time

from evaluator import best_pair_minimax_depth2, cache_stats, format_profile
from ps_driver import PSDriver


DEPTH2_TOP_K = 50
P2_POLICY = "minimax"  # "minimax" = adversarial; "greedy" = scripted Subway-AI model


TERRAKION = {
    "species": "Terrakion", "item": "Wide Lens", "ability": "Justified",
    "moves": ["Rock Slide", "Sacred Sword", "X-Scissor", "Protect"],
    "nature": "Jolly", "evs": {"atk": 252, "spd": 4, "spe": 252},
}
WHIMSICOTT = {
    "species": "Whimsicott", "item": "Focus Sash", "ability": "Prankster",
    "moves": ["Giga Drain", "Beat Up", "Tailwind", "Sunny Day"],
    "nature": "Timid", "evs": {"def": 4, "spa": 252, "spe": 252},
}
DARMANITAN = {
    "species": "Darmanitan", "item": "Life Orb", "ability": "Sheer Force",
    "moves": ["Flare Blitz", "Rock Slide", "Earthquake", "Protect"],
    "nature": "Adamant", "evs": {"atk": 252, "spd": 4, "spe": 252},
}
THUNDURUS = {
    "species": "Thundurus", "item": "Expert Belt", "ability": "Prankster",
    "moves": ["Thunderbolt", "Grass Knot", "Psychic", "Dark Pulse"],
    "nature": "Timid", "ivs": {"atk": 0},
    "evs": {"def": 4, "spa": 252, "spe": 252},
}

# Sample opponent team from the Super Doubles pool — picked arbitrarily for
# the demo; not tuned for realism.
GARCHOMP = {
    "species": "Garchomp", "item": "Yache Berry", "ability": "Sand Veil",
    "moves": ["Earthquake", "Dragon Claw", "Rock Slide", "Protect"],
    "nature": "Jolly", "evs": {"atk": 252, "spd": 4, "spe": 252},
}
LATIOS = {
    "species": "Latios", "item": "Life Orb", "ability": "Levitate",
    "moves": ["Draco Meteor", "Psychic", "Thunderbolt", "Protect"],
    "nature": "Timid", "ivs": {"atk": 0}, "evs": {"def": 4, "spa": 252, "spe": 252},
}
METAGROSS = {
    "species": "Metagross", "item": "Occa Berry", "ability": "Clear Body",
    "moves": ["Meteor Mash", "Zen Headbutt", "Earthquake", "Protect"],
    "nature": "Adamant", "evs": {"hp": 4, "atk": 252, "spe": 252},
}
HYDREIGON = {
    "species": "Hydreigon", "item": "Choice Scarf", "ability": "Levitate",
    "moves": ["Draco Meteor", "Dark Pulse", "Flamethrower", "Surf"],
    "nature": "Timid", "ivs": {"atk": 0}, "evs": {"def": 4, "spa": 252, "spe": 252},
}


LICKILICKY = {
        "species": "Lickilicky",
        "item": "Sitrus Berry",
        "moves": [
          "Thunderbolt",
          "Ice Beam",
          "Me First",
          "Psych Up"
        ],
        "nature": "Naive",
        "evs": {
          "hp": 170,
          "attack": 0,
          "defense": 170,
          "sp_atk": 170,
          "sp_def": 0,
          "speed": 0
        }
      }

MAGMORTAR = {
        "species": "Magmortar",
        "item": "Passho Berry",
        "moves": [
          "Flame Charge",
          "Earthquake",
          "Confuse Ray",
          "Barrier"
        ],
        "nature": "Adamant",
        "evs": {
          "hp": 255,
          "attack": 255,
          "defense": 0,
          "sp_atk": 0,
          "sp_def": 0,
          "speed": 0
        }
      }

P1_TEAM = [TERRAKION, WHIMSICOTT, DARMANITAN, THUNDURUS]
P2_TEAM = [GARCHOMP, LATIOS, METAGROSS, HYDREIGON]
#P2_TEAM = [LICKILICKY, MAGMORTAR, METAGROSS, HYDREIGON]

MAX_TURNS = 60


def format_action(a):
    if a is None:
        return "-"
    if a["type"] == "pass":
        return "pass"
    if a["type"] == "switch":
        return f"switch->{a['to']}"
    tgt = f" @{a['target']}" if "target" in a else ""
    return f"{a['move']}{tgt}"


def format_side(actions):
    if actions is None:
        return "(no request)"
    return " | ".join(format_action(a) for a in actions)


def format_boosts(boosts):
    if not boosts: return ""
    parts = [f"{k}{v:+d}" for k, v in boosts.items() if v]
    return f" [{' '.join(parts)}]" if parts else ""


def format_mon(mon, show_hp_if_full=False):
    name = mon["set"]["name"]
    if mon.get("fainted"):
        return f"{name} FNT"
    hp, maxhp = mon["hp"], mon["maxhp"]
    pct = int(round(100 * hp / maxhp)) if maxhp else 0
    parts = [name]
    if pct < 100 or show_hp_if_full:
        parts.append(f"{pct}%")
    if mon.get("status"):
        parts.append(mon["status"])
    out = " ".join(parts) + format_boosts(mon.get("boosts", {}))
    return out


def format_side_state(side):
    actives = [m for m in side["pokemon"] if m.get("isActive")]
    bench = [m for m in side["pokemon"] if not m.get("isActive")]
    active_str = "  ".join(format_mon(m, show_hp_if_full=True) for m in actives)
    bench_str = ", ".join(format_mon(m) for m in bench) if bench else "-"
    return f"active: {active_str}  | bench: {bench_str}"


def rng_summary(events):
    if not events:
        return ""
    counts = {}
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    unknowns = [e["caller"] for e in events if e["kind"] == "unknown"]
    parts = [f"{k}:{v}" for k, v in sorted(counts.items())]
    summary = " ".join(parts)
    if unknowns:
        summary += f"  [unknown callers: {sorted(set(unknowns))}]"
    return summary


def main():
    with PSDriver() as drv:
        start = drv.start_battle(P1_TEAM, P2_TEAM)
        state = start["state"]

        print(f"P1: {[m['species'] for m in P1_TEAM]}")
        print(f"P2: {[m['species'] for m in P2_TEAM]}")
        print()

        step = 0
        while step < MAX_TURNS:
            step += 1
            enum = drv.enumerate_turn(state)
            req = enum.get("requestState")
            pairs = enum.get("action_pairs", [])
            if not pairs:
                print(f"[step {step}] no enumerable actions (requestState={req}); stopping")
                break

            t0 = time.perf_counter()
            pair, scored, profile = best_pair_minimax_depth2(
                drv, state, enum=enum, top_k=DEPTH2_TOP_K, p2_policy=P2_POLICY,
            )
            elapsed = time.perf_counter() - t0
            if pair is None:
                print(f"[step {step}] minimax returned no pair; stopping")
                break

            tag = "switch" if req == "switch" else "picking"
            stats = cache_stats(drv)
            print(f"[{tag:>8}] top-{DEPTH2_TOP_K} depth-2 in {elapsed:.1f}s (cache: {stats['entries']} entries)")
            print(f"         profile: {format_profile(profile)}")
            for p1_action, d1, d2, adv_p2 in scored:
                marker = "->" if p1_action == pair["p1"] else "  "
                print(f"         {marker} {format_side(p1_action):<48}  d1={d1:+.2f}  d2={d2:+.2f}  vs {format_side(adv_p2)}")

            turn_result = drv.execute_turn(state, {"p1": pair["p1"], "p2": pair["p2"]})
            state = turn_result["state"]

            tag = "switch" if req == "switch" else f"turn {turn_result['turn']}"
            print(f"[{tag:>8}] P1 {format_side(pair['p1']):<48} || P2 {format_side(pair['p2']):<48}"
                  f"  rng[{rng_summary(turn_result['rng_events'])}]")
            sides = turn_result["state"]["sides"]
            print(f"           P1 {format_side_state(sides[0])}")
            print(f"           P2 {format_side_state(sides[1])}")

            if turn_result["ended"]:
                print(f"\nbattle ended on step {step}. winner: {turn_result['winner']}. turn counter: {turn_result['turn']}")
                return 0
        else:
            print(f"\nreached MAX_TURNS={MAX_TURNS} without termination")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
