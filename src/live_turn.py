"""Live-play decision assist: given an observed battle state, compute the
engine's recommended action for the current turn with full top-K scoring.

Two modes:

**Gospel mode (default)** — full P2 team known. Reads a JSON scenario from
stdin (or `--scenario file.json`), starts a fresh PS battle with the supplied
teams, mutates the serialized state to match any observed conditions
(HP / status / boosts / weather / Trick Room / Tailwind), and runs a single
`best_pair_minimax_depth2` pass. Prints the same top-K scored table rollouts
produce, plus the chosen action in copy-pasteable form.

    python src/live_turn.py --scenario scenario.json
    cat scenario.json | python src/live_turn.py

**List-consistent mode** — trainer-aware filter (Path C scaffolding). Given a
trainer id and what you've seen so far, enumerates every (draw × ability-perm)
whose P2 team is consistent with your observations, so you can pick a
candidate back to plug into gospel mode.

    python src/live_turn.py --list-consistent --trainer-id 165 \
        --revealed-leads "Audino,Chansey"
    python src/live_turn.py --list-consistent --trainer-id 165 \
        --revealed-leads "Audino,Chansey" --show-config 1

Scenario JSON shape (all "state" / "search" keys are optional):

    {
      "header": ["optional description lines"],
      "p1_team": [...4 sets...],        // optional, defaults to CLAUDE.md team
      "p2_team": [...4 sets...],        // required
      "p1_leads": [0, 1],               // indices into p1_team, default [0,1]
      "p2_leads": [0, 1],
      "request": "move",                // "move" (default) or "switch" — for the
                                        // post-KO replacement query, set "switch"
                                        // and mark the dead active(s) with fainted=true
      "state": {
        "p1": {
          "active": [
            // active overrides: hp_frac, status, boosts, stall_counter, fainted
            // - stall_counter=N (consecutive prior Protect/Detect uses): N=1 → next
            //   succeeds 1/2, N=2 → 1/4, etc. Set this when you used Protect last turn.
            // - fainted=true on an active is allowed; combine with "request":"switch"
            //   for the immediate switch-in query, or use "request":"move" if the
            //   slot is permanently empty (no live replacement) and you want the
            //   one-mon-active move phase.
            {"hp_frac":1.0,"status":null,"boosts":{"atk":1},"stall_counter":1},
            {"fainted":true}
          ],
          "bench":  [{"hp_frac":1.0,"status":null}, {"fainted":true}]
        },
        "p2": {"active":[...], "bench":[...]},
        "field": {
          "weather": null,              // or "sunnyday"/"raindance"/"sandstorm"/"hail"
          "weather_turns": 0,
          "trickroom_turns": 0,
          "p1_tailwind_turns": 0,
          "p2_tailwind_turns": 0,
          "p1_reflect_turns": 0,        // physical halver (2/3× in doubles)
          "p1_lightscreen_turns": 0,    // special halver (2/3× in doubles)
          "p2_reflect_turns": 0,
          "p2_lightscreen_turns": 0
        }
      },
      "search": {
        "top_k": 30,                    // default 30 (single-turn latency budget)
        "assume_hit": true,
        "extended_scores": true,
        "p2_policy": "minimax",
        "seed": null                    // or [u16,u16,u16,u16]
      }
    }
"""
import argparse
import itertools
import json
import sys
import time
from typing import Dict, List, Optional, Tuple

from demo_trainer_rollout import P1_TEAM
from evaluator import best_pair_minimax_depth2, cache_stats, format_profile
from ps_driver import PSDriver
from rollout import format_side, format_side_state
from scraper import load_trainers
from trainer_adapter import adapt_pokemon, load_abilities


DEFAULT_TOP_K = 30  # generous; single-turn mode has more latency budget than rollouts


def _reorder_for_leads(team: List[Dict], leads: List[int]) -> List[Dict]:
    """PS custom-game starts with positions 0 and 1 as active. Reorder the team
    so the requested lead indices occupy those slots, preserving the other mons'
    relative order."""
    if len(leads) != 2 or leads[0] == leads[1]:
        raise SystemExit(f"leads must be two distinct indices, got {leads}")
    n = len(team)
    if any(i < 0 or i >= n for i in leads):
        raise SystemExit(f"lead indices out of range for {n}-mon team: {leads}")
    rest = [i for i in range(n) if i not in leads]
    return [team[i] for i in list(leads) + rest]


# --- state-override helpers ---------------------------------------------------

def _apply_mon_overrides(mon: Dict, override: Dict, *, is_active: bool,
                          slot_ref: str = "p1a") -> None:
    """Mutate a single serialized pokemon entry in place. Bench and active
    slots both accept fainted=true; on an active slot it means the mon is
    KO'd (use with request='switch' for the replacement query, or with the
    default request='move' when no live replacement remains and the slot
    stays empty for the rest of the battle)."""
    if override is None:
        return
    if override.get("fainted"):
        mon["hp"] = 0
        mon["fainted"] = True
        mon["status"] = ""  # PS clears status on faint; post-faint state shows ''
        mon["isActive"] = False
        # Volatiles don't survive a faint; clear them so leftover state from
        # start_battle's pre-turn-1 setup doesn't haunt the post-KO state.
        mon["volatiles"] = {}
        return
    if "hp_frac" in override and override["hp_frac"] is not None:
        frac = float(override["hp_frac"])
        if frac < 0 or frac > 1:
            raise SystemExit(f"hp_frac must be in [0,1], got {frac}")
        if frac == 0:
            raise SystemExit(
                "hp_frac=0 — use fainted=true for a KO'd mon (clears status + "
                "volatiles); hp_frac=0 alone leaves the mon technically alive "
                "and PS would heal it on switch-in"
            )
        mon["hp"] = max(1, int(round(mon["maxhp"] * frac)))
    if override.get("status"):
        # 'par' | 'brn' | 'psn' | 'tox' | 'slp' | 'frz'
        mon["status"] = override["status"]
    if "boosts" in override and override["boosts"]:
        for k, v in override["boosts"].items():
            if k not in mon["boosts"]:
                raise SystemExit(f"unknown boost key {k!r}; valid: {list(mon['boosts'])}")
            mon["boosts"][k] = int(v)
    if "stall_counter" in override and override["stall_counter"]:
        if not is_active:
            raise SystemExit("stall_counter only meaningful on active slots")
        n = int(override["stall_counter"])
        if n < 1:
            raise SystemExit(f"stall_counter must be >= 1, got {n}")
        # Gen 5 stall doubles the counter per consecutive Protect: n=1 → counter=2
        # (next Protect succeeds 1/2), n=2 → counter=4 (1/4), n=3 → counter=8.
        # Matches the empirical shape from src/probe_state_shapes.py.
        mon.setdefault("volatiles", {})["stall"] = {
            "id": "stall", "name": "stall",
            "target": f"[Pokemon:{slot_ref}]",
            "source": f"[Pokemon:{slot_ref}]", "sourceSlot": slot_ref,
            "sourceEffect": {
                "hit": 1, "totalDamage": 0, "lastHit": True,
                "hitTargets": [f"[Pokemon:{slot_ref}]"],
                "move": "[Move:protect]",
            },
            "duration": 1, "counter": 2 ** n,
            "effectOrder": 9,
        }


def _apply_side_state(state: Dict, side_idx: int, side_overrides: Dict) -> None:
    if not side_overrides:
        return
    side = state["sides"][side_idx]
    pokemons = side["pokemon"]
    side_letter = "p1" if side_idx == 0 else "p2"
    # positions 0/1 = actives, 2/3 = bench (ordering matches the reordered team we built)
    for i, o in enumerate(side_overrides.get("active", [])[:2]):
        slot_ref = f"{side_letter}{'a' if i == 0 else 'b'}"
        _apply_mon_overrides(pokemons[i], o, is_active=True, slot_ref=slot_ref)
    for i, o in enumerate(side_overrides.get("bench", [])[:2]):
        _apply_mon_overrides(pokemons[2 + i], o, is_active=False)
    # Recompute alive/fainted counters so PS's forced-switch logic sees the
    # right bench. `pokemonLeft` gates whether a side has lost the battle.
    fainted_count = sum(1 for m in pokemons if m.get("fainted"))
    side["totalFainted"] = fainted_count
    side["pokemonLeft"] = len(pokemons) - fainted_count


def _setup_switch_request(state: Dict) -> None:
    """Mark the battle as in switch phase.

    Two PS internals matter:
    1. `state.requestState = 'switch'` → top-level battle phase.
    2. `pokemon.switchFlag = true` on each fainted active that needs a
       replacement. PS's `Battle.getRequests('switch')` builds the
       per-side `forceSwitch` array from `side.active.map(p => !!p.switchFlag)`
       (sim/battle.ts:1424), NOT from `pokemon.fainted`. So just marking a
       mon fainted isn't enough — we have to set switchFlag too.
    3. `side.activeRequest`: PS's deserializer (sim/state.ts:133-148)
       treats `undefined` as "recompute via getRequests" and `null` as a
       tombstone meaning "no request". We delete the key for sides with a
       fainted active + live bench (so PS recomputes), and set null for
       sides with no pending switch.
    """
    state["requestState"] = "switch"
    for side in state["sides"]:
        actives = side["pokemon"][:2]
        bench = side["pokemon"][2:]
        bench_alive = sum(1 for m in bench if not m.get("fainted"))
        force_flags = [bool(m.get("fainted")) for m in actives]
        for i, flag in enumerate(force_flags):
            actives[i]["switchFlag"] = bool(flag and bench_alive > 0)
        if any(force_flags) and bench_alive > 0:
            side.pop("activeRequest", None)  # → undefined → PS recomputes
        else:
            side["activeRequest"] = None
        # Keep choice counters consistent (applyChoicesInPlace's makeChoices
        # validates against them).
        force_count = min(sum(force_flags), bench_alive)
        side.setdefault("choice", {})
        side["choice"]["forcedSwitchesLeft"] = force_count
        side["choice"]["forcedPassesLeft"] = 0
        side["choice"]["actions"] = []
        side["choice"]["error"] = ""
        side["choice"]["cantUndo"] = False
        side["choice"]["switchIns"] = []


def _apply_field(state: Dict, field: Dict) -> None:
    if not field:
        return
    # Source attribution uses the side's slot-0 active; PS needs well-formed
    # bracket refs to deserialize, but the attribution doesn't affect gameplay.
    if field.get("p1_tailwind_turns"):
        state["sides"][0]["sideConditions"]["tailwind"] = {
            "id": "tailwind", "target": "[Side:p1]",
            "source": "[Pokemon:p1a]", "sourceSlot": "p1a",
            "duration": int(field["p1_tailwind_turns"]), "effectOrder": 12,
        }
    if field.get("p2_tailwind_turns"):
        state["sides"][1]["sideConditions"]["tailwind"] = {
            "id": "tailwind", "target": "[Side:p2]",
            "source": "[Pokemon:p2a]", "sourceSlot": "p2a",
            "duration": int(field["p2_tailwind_turns"]), "effectOrder": 12,
        }
    if field.get("trickroom_turns"):
        state["field"]["pseudoWeather"]["trickroom"] = {
            "id": "trickroom",
            "source": "[Pokemon:p1a]", "sourceSlot": "p1a",
            "duration": int(field["trickroom_turns"]), "effectOrder": 0,
        }
    if field.get("weather"):
        state["field"]["weather"] = field["weather"]
        state["field"]["weatherState"] = {
            "id": field["weather"], "effectOrder": 0,
            "source": "[Pokemon:p1a]", "sourceSlot": "p1a",
            "duration": int(field.get("weather_turns", 4)),
            "target": "[Field]",
        }
    # Reflect / Light Screen: PS's getDamage pipeline applies the screen
    # multiplier (1/2 in singles, 2/3 in doubles) when these conditions are
    # present in `side.sideConditions`, so the static damage matrix picks them
    # up automatically — no eval-side code change needed.
    for side_idx, prefix in [(0, "p1"), (1, "p2")]:
        for screen_id, key_suffix in [("reflect", "reflect_turns"),
                                       ("lightscreen", "lightscreen_turns")]:
            turns = field.get(f"{prefix}_{key_suffix}")
            if turns:
                state["sides"][side_idx]["sideConditions"][screen_id] = {
                    "id": screen_id, "target": f"[Side:{prefix}]",
                    "source": f"[Pokemon:{prefix}a]", "sourceSlot": f"{prefix}a",
                    "duration": int(turns), "effectOrder": 1,
                }


# --- gospel-mode entry --------------------------------------------------------

def run_gospel(scenario: Dict) -> int:
    p1_team = scenario.get("p1_team") or P1_TEAM
    p2_team = scenario.get("p2_team")
    if not p2_team or len(p2_team) != 4:
        raise SystemExit("scenario.p2_team must be exactly 4 full PS sets")

    p1_leads = scenario.get("p1_leads", [0, 1])
    p2_leads = scenario.get("p2_leads", [0, 1])
    p1_ordered = _reorder_for_leads(p1_team, p1_leads)
    p2_ordered = _reorder_for_leads(p2_team, p2_leads)

    request_kind = scenario.get("request", "move")
    if request_kind not in ("move", "switch"):
        raise SystemExit(f"scenario.request must be 'move' or 'switch', got {request_kind!r}")

    search = scenario.get("search", {}) or {}
    top_k = int(search.get("top_k", DEFAULT_TOP_K))
    assume_hit = bool(search.get("assume_hit", True))
    extended = bool(search.get("extended_scores", True))
    p2_policy = search.get("p2_policy", "minimax")
    seed = search.get("seed")

    for line in scenario.get("header", []) or []:
        print(line)
    print(f"P1 leads: {[m['species'] for m in p1_ordered[:2]]}  "
          f"bench: {[m['species'] for m in p1_ordered[2:]]}")
    print(f"P2 leads: {[m['species'] for m in p2_ordered[:2]]}  "
          f"bench: {[m['species'] for m in p2_ordered[2:]]}")
    field = (scenario.get("state") or {}).get("field") or {}
    print(f"Field: weather={field.get('weather') or 'none'}"
          f"({field.get('weather_turns', 0)})  "
          f"TR={field.get('trickroom_turns', 0)}  "
          f"p1_TW={field.get('p1_tailwind_turns', 0)}  "
          f"p2_TW={field.get('p2_tailwind_turns', 0)}  "
          f"p1_R/LS={field.get('p1_reflect_turns', 0)}/{field.get('p1_lightscreen_turns', 0)}  "
          f"p2_R/LS={field.get('p2_reflect_turns', 0)}/{field.get('p2_lightscreen_turns', 0)}")
    print()

    with PSDriver() as drv:
        start = drv.start_battle(p1_ordered, p2_ordered, seed=seed)
        state = start["state"]

        state_overrides = scenario.get("state") or {}
        _apply_side_state(state, 0, state_overrides.get("p1"))
        _apply_side_state(state, 1, state_overrides.get("p2"))
        _apply_field(state, state_overrides.get("field"))
        if request_kind == "switch":
            _setup_switch_request(state)

        # Mutated state is still 'move' request at turn 1 (or 'switch' if the
        # caller asked); best_pair_minimax consumes the serialized state directly,
        # no re-serialization needed.
        print(f"Phase: {request_kind}")
        print(f"P1 state: {format_side_state(state['sides'][0])}")
        print(f"P2 state: {format_side_state(state['sides'][1])}")
        print()

        t0 = time.perf_counter()
        pair, scored, profile = best_pair_minimax_depth2(
            drv, state, top_k=top_k, p2_policy=p2_policy,
            assume_hit=assume_hit, extended_scores=extended,
        )
        elapsed = time.perf_counter() - t0

        if pair is None:
            print("engine returned no pair (no legal actions?)")
            return 1

        stats = cache_stats(drv)
        print(f"[picking ] top-{top_k} depth-2 in {elapsed:.2f}s "
              f"(cache: {stats['entries']} entries, assume_hit={assume_hit}, "
              f"extended={extended}, p2_policy={p2_policy})")
        print(f"           profile: {format_profile(profile)}")
        for entry in scored:
            p1_action, d1, d2, adv_p2 = entry[0], entry[1], entry[2], entry[3]
            marker = "->" if p1_action == pair["p1"] else "  "
            if extended and len(entry) >= 6:
                d2_worst, d2_expected = entry[4], entry[5]
                print(f"           {marker} {format_side(p1_action):<52}  "
                      f"d1={d1:+.2f}  d2={d2:+.2f}  worst={d2_worst:+.2f}  "
                      f"exp={d2_expected:+.2f}  vs {format_side(adv_p2)}")
            else:
                print(f"           {marker} {format_side(p1_action):<52}  "
                      f"d1={d1:+.2f}  d2={d2:+.2f}  vs {format_side(adv_p2)}")

        print()
        print("===== CHOSEN =====")
        # In switch phase, the side without a request gets pair['p1']/['p2']=None.
        if pair.get("p1") is None:
            print("P1: (no request — opposing side's switch only)")
        else:
            for i, a in enumerate(pair["p1"]):
                print(f"P1 slot{i}: {format_side([a])}")
        print()
        print("raw action JSON (for execute_turn):")
        print(json.dumps({"p1": pair["p1"], "p2_adversarial": pair["p2"]},
                         indent=2, default=str))
        return 0


# --- list-consistent mode -----------------------------------------------------

def _species_list(trainer, draw: Tuple[int, ...]) -> List[str]:
    return [trainer.pokemonList[i].name for i in draw]


def _enumerate_consistent(trainer, abilities_table, revealed_leads: List[str],
                          revealed_back: List[str]):
    """Yield (draw_tuple, lead_tuple, ability_tuple, p2_team_sets) for every
    config where the drawn species multiset covers revealed species and some
    ordering of two drawn mons matches revealed_leads in any order."""
    pool = len(trainer.pokemonList)
    revealed_lead_ms = sorted(s.lower() for s in revealed_leads)
    revealed_back_ms = sorted(s.lower() for s in revealed_back)

    for draw in itertools.combinations(range(pool), 4):
        species = [_species_list(trainer, draw)[i].lower() for i in range(4)]
        species_ms = sorted(species)
        # All revealed species (leads + back) must be present in the draw,
        # counted with multiplicity — so a Veteran with 2 Latios slots survives
        # a revealed "Latios, Latios".
        needed = sorted(revealed_lead_ms + revealed_back_ms)
        remaining = list(species_ms)
        ok = True
        for s in needed:
            if s in remaining:
                remaining.remove(s)
            else:
                ok = False
                break
        if not ok:
            continue

        # Lead pair must match revealed_leads as a multiset. Iterate all (i,j)
        # lead choices and filter to those whose species match.
        for lead in itertools.combinations(range(4), 2):
            lead_ms = sorted([species[lead[0]], species[lead[1]]])
            if lead_ms != revealed_lead_ms:
                continue
            # Resolve ability fan-out per drawn mon.
            ability_options_per_slot = []
            for pool_idx in draw:
                bs = trainer.pokemonList[pool_idx]
                raw = {"name": bs.name, "item": bs.item, "nature": bs.nature,
                       "moves": bs.moves, "evs": bs.evs}
                variants = adapt_pokemon(raw, abilities_table)
                ability_options_per_slot.append(variants)
            for ab_idxs in itertools.product(
                    *(range(len(v)) for v in ability_options_per_slot)):
                p2_team_sets = [
                    ability_options_per_slot[slot][ab_idxs[slot]]
                    for slot in range(4)
                ]
                yield draw, lead, ab_idxs, p2_team_sets


def run_list_consistent(args) -> int:
    trainers = load_trainers()
    trainer = next((t for t in trainers if t.internalId == args.trainer_id), None)
    if trainer is None:
        raise SystemExit(f"no trainer with internalId={args.trainer_id}")
    abilities_table = load_abilities()

    revealed_leads = [s.strip() for s in (args.revealed_leads or "").split(",") if s.strip()]
    revealed_back = [s.strip() for s in (args.revealed_back or "").split(",") if s.strip()]
    if len(revealed_leads) != 2:
        raise SystemExit(f"--revealed-leads must be 2 comma-separated species, got {revealed_leads!r}")

    print(f"trainer #{trainer.internalId} {trainer.trainerClass} {trainer.name} "
          f"(pool={len(trainer.pokemonList)}, course={trainer.course.name})")
    print(f"revealed leads: {revealed_leads}"
          + (f"  revealed back: {revealed_back}" if revealed_back else ""))
    print()

    configs = list(_enumerate_consistent(trainer, abilities_table,
                                          revealed_leads, revealed_back))
    if not configs:
        print("no consistent configurations — check species spelling "
              "against data/trainers.json, or this trainer can't produce "
              "that combination")
        return 1

    print(f"{len(configs)} consistent matchups "
          f"(draws × lead-assignments × ability-perms)")
    print()

    if args.show_config is not None:
        if args.show_config < 1 or args.show_config > len(configs):
            raise SystemExit(f"--show-config must be in [1,{len(configs)}]")
        draw, lead, abs_, p2_team = configs[args.show_config - 1]
        species = _species_list(trainer, draw)
        print(f"[#{args.show_config}] draw-indices={list(draw)} "
              f"draw-species={species} "
              f"lead-positions={list(lead)} "
              f"abilities={[m['ability'] for m in p2_team]}")
        print()
        print("scenario snippet (paste into your scenario JSON):")
        snippet = {
            "p2_team": p2_team,
            "p2_leads": list(lead),
        }
        print(json.dumps(snippet, indent=2, default=str))
        return 0

    # Summary listing. Sort by draw, group by species tuple so multi-set
    # trainers collapse to readable rows when sets vary but species don't.
    max_show = args.max_show
    for i, (draw, lead, abs_, p2_team) in enumerate(configs[:max_show], 1):
        species = _species_list(trainer, draw)
        lead_species = [species[lead[0]], species[lead[1]]]
        abilities = [m["ability"] for m in p2_team]
        print(f"[#{i:>3}] draw={list(draw)}  "
              f"species={species}  "
              f"lead={lead_species}  "
              f"abilities={abilities}")
    if len(configs) > max_show:
        print(f"... {len(configs) - max_show} more (pass --max-show to extend)")
    print()
    print("use --show-config N to dump the p2_team JSON for config #N")
    return 0


# --- entry point --------------------------------------------------------------

def _load_scenario(path: Optional[str]) -> Dict:
    if path:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    data = sys.stdin.read()
    if not data.strip():
        raise SystemExit("no scenario provided — pass --scenario FILE or pipe JSON on stdin")
    return json.loads(data)


def main():
    parser = argparse.ArgumentParser()
    # gospel mode (default)
    parser.add_argument("--scenario", help="path to scenario JSON; omit to read from stdin")
    # list-consistent mode
    parser.add_argument("--list-consistent", action="store_true",
                        help="print (draw × lead × ability) configs consistent with observations")
    parser.add_argument("--trainer-id", type=int, help="trainer internalId (list-consistent mode)")
    parser.add_argument("--revealed-leads", default="",
                        help="comma-separated species seen in p2 lead slots (order-insensitive)")
    parser.add_argument("--revealed-back", default="",
                        help="comma-separated species seen in p2 bench (e.g. on a forced switch)")
    parser.add_argument("--max-show", type=int, default=50,
                        help="max consistent configs to print (default 50)")
    parser.add_argument("--show-config", type=int, default=None,
                        help="dump the full p2_team JSON for the Nth consistent config")
    args = parser.parse_args()

    if args.list_consistent:
        if args.trainer_id is None:
            parser.error("--trainer-id is required for --list-consistent")
        return run_list_consistent(args)

    scenario = _load_scenario(args.scenario)
    return run_gospel(scenario)


if __name__ == "__main__":
    raise SystemExit(main())
