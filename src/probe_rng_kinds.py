"""Probe unknown RNG callers.

Stages a series of short battles that each deliberately exercise one class of
RNG-producing mechanic. Runs them, collects any `rng_events` with kind='unknown',
and reports unique callers at the end. Findings get mapped into KIND_BY_CALLER
in driver/driver.js.

Covered:
  multi_hit     - Icicle Spear (no Skill Link) hits 2-5 times via sample
  paralysis     - fullpara roll on a paralyzed mon's action
  sleep         - wake roll + sleep-duration pick
  confusion     - confuse self-hit roll + confusion-duration pick
  contact_abil  - Static proc on contact (30% para)
"""
from collections import defaultdict

from ps_driver import PSDriver


# Bulky generic filler used as slot1 punching bag / spare.
BLISSEY = {
    "species": "Blissey", "item": "Leftovers", "ability": "Natural Cure",
    "moves": ["Soft-Boiled", "Seismic Toss", "Protect", "Toxic"],
    "nature": "Sassy", "evs": {"hp": 252, "def": 4, "spd": 252},
}


def run_probe(name, p1_team, p2_team, turns_actions):
    print(f"\n=== {name} ===")
    unknowns = []
    counts = defaultdict(int)
    with PSDriver() as drv:
        state = drv.start_battle(p1_team, p2_team)["state"]
        for i, actions in enumerate(turns_actions):
            res = drv.execute_turn(state, actions)
            state = res["state"]
            for e in res["rng_events"]:
                counts[e["kind"]] += 1
                if e["kind"] == "unknown":
                    unknowns.append((i, e["caller"], e.get("effect"), e["primitive"], e["args"]))
            if res.get("ended"):
                break
    print(f"  events: {' '.join(f'{k}:{v}' for k,v in sorted(counts.items())) or '(none)'}")
    for t, caller, effect, prim, args in unknowns:
        print(f"  turn {t} unknown: {caller} effect={effect!r} primitive={prim} args={args}")
    return unknowns


def probe_multi_hit():
    # Cloyster w/ Shell Armor (not Skill Link) → Icicle Spear rolls hit count.
    cloyster = {
        "species": "Cloyster", "item": "Never-Melt Ice", "ability": "Shell Armor",
        "moves": ["Icicle Spear", "Rock Blast", "Protect", "Surf"],
        "nature": "Adamant", "evs": {"atk": 252, "hp": 4, "spe": 252},
    }
    # Don't let p2 Protect — that blocks the move and skips the hit-count roll.
    return run_probe("multi_hit (Icicle Spear)",
        [cloyster, BLISSEY], [BLISSEY, BLISSEY],
        [{"p1": [{"type": "move", "move": "Icicle Spear", "target": "foeSlot0"},
                 {"type": "move", "move": "Soft-Boiled"}],
          "p2": [{"type": "move", "move": "Soft-Boiled"},
                 {"type": "move", "move": "Soft-Boiled"}]}])


def probe_paralysis():
    jolteon = {
        "species": "Jolteon", "item": "", "ability": "Volt Absorb",
        "moves": ["Thunder Wave", "Thunderbolt", "Quick Attack", "Protect"],
        "nature": "Timid", "evs": {"spa": 252, "spe": 252, "def": 4},
    }
    scrafty = {
        "species": "Scrafty", "item": "Leftovers", "ability": "Moxie",
        "moves": ["Drain Punch", "Crunch", "Protect", "Fake Out"],
        "nature": "Adamant", "evs": {"hp": 252, "atk": 4, "spd": 252},
    }
    return run_probe("paralysis (fullpara roll)",
        [jolteon, BLISSEY], [scrafty, BLISSEY],
        [
            # T1: Jolteon T-Wave Scrafty; Scrafty attacks normally.
            {"p1": [{"type": "move", "move": "Thunder Wave", "target": "foeSlot0"},
                    {"type": "move", "move": "Protect"}],
             "p2": [{"type": "move", "move": "Drain Punch", "target": "foeSlot0"},
                    {"type": "move", "move": "Protect"}]},
            # T2: Scrafty paralyzed; its attack triggers fullpara roll.
            {"p1": [{"type": "move", "move": "Protect"},
                    {"type": "move", "move": "Protect"}],
             "p2": [{"type": "move", "move": "Drain Punch", "target": "foeSlot0"},
                    {"type": "move", "move": "Protect"}]},
        ])


def probe_sleep():
    breloom = {
        "species": "Breloom", "item": "Toxic Orb", "ability": "Technician",
        "moves": ["Spore", "Mach Punch", "Protect", "Bullet Seed"],
        "nature": "Jolly", "evs": {"atk": 252, "spe": 252, "def": 4},
    }
    # Don't let p2 Protect on T1 — that blocks Spore and skips the duration pick.
    return run_probe("sleep (duration pick + wake roll)",
        [breloom, BLISSEY], [BLISSEY, BLISSEY],
        [
            # T1: Spore on foeSlot0 → Blissey asleep (duration picked).
            {"p1": [{"type": "move", "move": "Spore", "target": "foeSlot0"},
                    {"type": "move", "move": "Soft-Boiled"}],
             "p2": [{"type": "move", "move": "Soft-Boiled"},
                    {"type": "move", "move": "Soft-Boiled"}]},
            # T2–T3: sleeping Blissey tries to attack (onBeforeMove decrement; no roll).
            {"p1": [{"type": "move", "move": "Protect"},
                    {"type": "move", "move": "Soft-Boiled"}],
             "p2": [{"type": "move", "move": "Seismic Toss", "target": "foeSlot0"},
                    {"type": "move", "move": "Soft-Boiled"}]},
            {"p1": [{"type": "move", "move": "Protect"},
                    {"type": "move", "move": "Soft-Boiled"}],
             "p2": [{"type": "move", "move": "Seismic Toss", "target": "foeSlot0"},
                    {"type": "move", "move": "Soft-Boiled"}]},
        ])


def probe_confusion():
    gengar = {
        "species": "Gengar", "item": "Black Sludge", "ability": "Levitate",
        "moves": ["Confuse Ray", "Shadow Ball", "Protect", "Thunderbolt"],
        "nature": "Timid", "evs": {"spa": 252, "spe": 252, "def": 4},
    }
    scrafty = {
        "species": "Scrafty", "item": "Leftovers", "ability": "Moxie",
        "moves": ["Drain Punch", "Crunch", "Protect", "Fake Out"],
        "nature": "Adamant", "evs": {"hp": 252, "atk": 4, "spd": 252},
    }
    return run_probe("confusion (self-hit roll + duration pick)",
        [gengar, BLISSEY], [scrafty, BLISSEY],
        [
            # T1: Confuse Scrafty.
            {"p1": [{"type": "move", "move": "Confuse Ray", "target": "foeSlot0"},
                    {"type": "move", "move": "Protect"}],
             "p2": [{"type": "move", "move": "Drain Punch", "target": "foeSlot0"},
                    {"type": "move", "move": "Protect"}]},
            # T2–T4: confused Scrafty attacks → confusion self-hit roll each turn.
            {"p1": [{"type": "move", "move": "Protect"},
                    {"type": "move", "move": "Protect"}],
             "p2": [{"type": "move", "move": "Drain Punch", "target": "foeSlot0"},
                    {"type": "move", "move": "Protect"}]},
            {"p1": [{"type": "move", "move": "Protect"},
                    {"type": "move", "move": "Protect"}],
             "p2": [{"type": "move", "move": "Drain Punch", "target": "foeSlot0"},
                    {"type": "move", "move": "Protect"}]},
        ])


def probe_contact_static():
    terrakion = {
        "species": "Terrakion", "item": "Wide Lens", "ability": "Justified",
        "moves": ["X-Scissor", "Rock Slide", "Sacred Sword", "Protect"],
        "nature": "Jolly", "evs": {"atk": 252, "spd": 4, "spe": 252},
    }
    jolteon = {
        "species": "Jolteon", "item": "Leftovers", "ability": "Static",
        "moves": ["Thunderbolt", "Protect", "Quick Attack", "Rest"],
        "nature": "Modest", "evs": {"hp": 252, "spa": 252, "def": 4},
    }
    # 3 turns of contact hits to get at least one Static proc attempt per turn.
    one_turn = {
        "p1": [{"type": "move", "move": "X-Scissor", "target": "foeSlot0"},
               {"type": "move", "move": "Protect"}],
        "p2": [{"type": "move", "move": "Protect"},
               {"type": "move", "move": "Protect"}],
    }
    return run_probe("contact_ability (Static 30% para on contact)",
        [terrakion, BLISSEY], [jolteon, BLISSEY],
        [one_turn, one_turn, one_turn])


def main():
    all_unknowns = []
    for probe in [probe_multi_hit, probe_paralysis, probe_sleep,
                  probe_confusion, probe_contact_static]:
        try:
            all_unknowns.extend(probe())
        except Exception as e:
            print(f"  ERROR: {e}")

    print("\n" + "=" * 60)
    print("SUMMARY: unique unknown callers")
    print("=" * 60)
    by_key = defaultdict(list)
    for t, caller, effect, prim, args in all_unknowns:
        by_key[(caller, effect)].append((prim, args))
    if not by_key:
        print("(none — all events classified)")
        return
    for (caller, effect), entries in sorted(by_key.items()):
        prims = sorted(set(p for p, _ in entries))
        print(f"  {caller} effect={effect!r}  primitives={prims}  count={len(entries)}")


if __name__ == "__main__":
    main()
