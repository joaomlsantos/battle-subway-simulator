"""Enumerate all matchup configurations for a single trainer and record
outcomes. Each matchup is one (P1 lead × P2 draw × P2 lead × P2 ability-perm)
combination; we run the shared rollout silently and append one result row to
`data/matchups/trainer-{id}.json` after every battle, rewriting atomically so
an interrupt leaves a valid file.

Scaling: pool size drives the draw count via C(pool, 4); SUPER_COURSE trainers
range from 7 to 88, so SUPER pool-88 (#111 Sherman) is ~2.4M draws before
leads/abilities — use `--limit` or pick a small-pool trainer for exhaustive
runs. P1 lead defaults to the primary (`0,1` = Terrakion + Whimsicott); pass
`--p1-leads all` to enumerate all 6 P1 lead configs.

Usage:
    python src/enum_matchups.py --trainer-id 165
    python src/enum_matchups.py --trainer-id 165 --p1-leads all
    python src/enum_matchups.py --trainer-id 165 --limit 20
"""
import argparse
import itertools
import json
import os
import random
import time
from datetime import datetime
from typing import Dict, Iterable, List, Tuple

from demo_trainer_rollout import (
    P1_TEAM, build_opponent_team, find_trainer,
)
from ps_driver import PSDriver
from rollout import run_rollout
from scraper import load_trainers
from trainer_adapter import adapt_pokemon, load_abilities


OUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "matchups"))


def p1_team_for_lead(p1_lead: Tuple[int, int]) -> List[Dict]:
    """Reorder P1_TEAM so lead[0] then lead[1] occupy the first two slots.
    PS custom-game battles start with the first two listed mons as the active
    leads, so this is how we pick which pair leads.
    """
    remaining = [i for i in range(4) if i not in p1_lead]
    return [P1_TEAM[i] for i in list(p1_lead) + remaining]


def ability_counts(trainer, draw_idxs: Iterable[int], abilities_table: Dict) -> List[int]:
    """How many regular-ability variants each drawn mon has (1 or 2)."""
    counts = []
    for idx in draw_idxs:
        bs = trainer.pokemonList[idx]
        raw = {"name": bs.name, "item": bs.item, "nature": bs.nature,
               "moves": bs.moves, "evs": bs.evs}
        counts.append(len(adapt_pokemon(raw, abilities_table)))
    return counts


def iter_configs(trainer, abilities_table, p1_leads: List[Tuple[int, int]], limit=None):
    """Yield matchup configs: {p1_lead, p2_draw, p2_lead, p2_abilities}."""
    pool = len(trainer.pokemonList)
    n = 0
    for p1_lead in p1_leads:
        for draw in itertools.combinations(range(pool), 4):
            counts = ability_counts(trainer, draw, abilities_table)
            for abilities in itertools.product(*(range(c) for c in counts)):
                for p2_lead in itertools.combinations(range(4), 2):
                    yield {
                        "p1_lead": list(p1_lead),
                        "p2_draw": list(draw),
                        "p2_lead": list(p2_lead),
                        "p2_abilities": list(abilities),
                    }
                    n += 1
                    if limit is not None and n >= limit:
                        return


def random_seed(rng: random.Random) -> List[int]:
    """PS PRNG takes 4 u16s. Sample a fresh one per battle so matchups don't
    all replay the same RNG sequence baked into the driver's DEFAULT_SEED."""
    return [rng.randrange(0, 65536) for _ in range(4)]


def matchup_label(trainer_id: int, cfg: Dict) -> str:
    """Compact tag for log filenames: t{id}_d{draw}_l{lead}_a{abs}.
    Uses single digits per slot since pool indices stay <10 in this codebase
    and ability indices are always 0/1."""
    draw = "".join(str(i) for i in cfg["p2_draw"])
    lead = "".join(str(i) for i in cfg["p2_lead"])
    abs_ = "".join(str(i) for i in cfg["p2_abilities"])
    p1_lead = "".join(str(i) for i in cfg["p1_lead"])
    return f"t{trainer_id}_p1l{p1_lead}_d{draw}_l{lead}_a{abs_}"


def run_one(drv, trainer, abilities_table, cfg, *, top_k, max_turns, assume_hit, seed):
    p1_team = p1_team_for_lead(tuple(cfg["p1_lead"]))
    p2_team = build_opponent_team(
        trainer, cfg["p2_draw"], cfg["p2_lead"], cfg["p2_abilities"], abilities_table
    )
    header = [
        f"Trainer: #{trainer.internalId} {trainer.trainerClass} {trainer.name} ({trainer.course.name})",
        f"p1_lead={cfg['p1_lead']}  p2_draw={cfg['p2_draw']}  "
        f"p2_lead={cfg['p2_lead']}  p2_abilities={cfg['p2_abilities']}",
    ]
    for mon in p2_team:
        header.append(
            f"  - {mon['species']} @ {mon['item']} | {mon['ability']} | {mon['nature']} | "
            f"{'/'.join(mon['moves'])}"
        )

    t0 = time.perf_counter()
    result = run_rollout(
        drv, p1_team, p2_team,
        header_lines=header,
        top_k=top_k,
        assume_hit=assume_hit,
        max_turns=max_turns,
        quiet=True,
        seed=seed,
        log_label=matchup_label(trainer.internalId, cfg),
    )
    elapsed = time.perf_counter() - t0

    return {
        **cfg,
        "p2_species": [m["species"] for m in p2_team],
        "p2_abilities_resolved": [m["ability"] for m in p2_team],
        "seed": seed,
        "winner": result["winner"],
        "turn": result["turn"],
        "steps": result["steps"],
        "ended": result["ended"],
        "elapsed_s": round(elapsed, 2),
    }


def save(out_path, trainer, config, matchups):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    payload = {
        "trainer": {
            "id": trainer.internalId,
            "class": trainer.trainerClass,
            "name": trainer.name,
            "course": trainer.course.name,
            "pool_size": len(trainer.pokemonList),
        },
        "config": config,
        "matchups": matchups,
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, out_path)


def parse_p1_leads(s: str) -> List[Tuple[int, int]]:
    if s == "all":
        return list(itertools.combinations(range(4), 2))
    parts = [int(x) for x in s.split(",") if x.strip()]
    if len(parts) != 2 or any(p < 0 or p >= 4 for p in parts) or parts[0] == parts[1]:
        raise SystemExit(f"invalid --p1-leads {s!r}: expected 'all' or two distinct indices in [0,3]")
    return [tuple(parts)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trainer-id", type=int, required=True)
    parser.add_argument("--p1-leads", default="0,1",
                        help="'all' for C(4,2)=6 lead configs, or 'a,b' for a single pair (default 0,1 = Terrakion+Whimsicott)")
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--top-k", type=int, default=15)
    parser.add_argument("--limit", type=int, default=None,
                        help="cap matchups to run (for smoke tests)")
    parser.add_argument("--no-assume-hit", action="store_true")
    parser.add_argument("--seed", type=int, default=None,
                        help="seed Python's RNG that derives per-battle PS seeds (for reproducible sweeps)")
    parser.add_argument("--out", default=None,
                        help="output JSON path (default: data/matchups/trainer-{id}.json)")
    args = parser.parse_args()

    trainers = load_trainers()
    trainer = find_trainer(trainers, args.trainer_id)
    abilities_table = load_abilities()
    p1_leads = parse_p1_leads(args.p1_leads)
    assume_hit = not args.no_assume_hit

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = args.out or os.path.join(OUT_DIR, f"trainer-{trainer.internalId}-{stamp}.json")
    rng = random.Random(args.seed)
    config = {
        "p1_lead_idxs": [list(p) for p in p1_leads],
        "assume_hit": assume_hit,
        "top_k": args.top_k,
        "max_turns": args.max_turns,
        "seed": args.seed,
    }

    plans = list(iter_configs(trainer, abilities_table, p1_leads, limit=args.limit))
    print(f"trainer #{trainer.internalId} {trainer.trainerClass} {trainer.name} "
          f"(pool={len(trainer.pokemonList)}): {len(plans)} matchups")
    print(f"writing to {out_path}")

    matchups = []
    with PSDriver() as drv:
        for i, cfg in enumerate(plans, 1):
            seed = random_seed(rng)
            row = run_one(drv, trainer, abilities_table, cfg,
                          top_k=args.top_k, max_turns=args.max_turns, assume_hit=assume_hit,
                          seed=seed)
            matchups.append(row)
            save(out_path, trainer, config, matchups)
            status = row["winner"] if row["ended"] else "TIMEOUT"
            print(f"  [{i:>4}/{len(plans)}] p1_lead={cfg['p1_lead']} "
                  f"draw={cfg['p2_draw']} lead={cfg['p2_lead']} ab={cfg['p2_abilities']} "
                  f"-> {status} (t={row['turn']}, {row['elapsed_s']:.1f}s)")

    wins = sum(1 for m in matchups if (m["winner"] or "").lower() == "p1")
    losses = sum(1 for m in matchups if (m["winner"] or "").lower() == "p2")
    timeouts = sum(1 for m in matchups if not m["ended"])
    print(f"\nsummary: p1_wins={wins}  p2_wins={losses}  timeouts={timeouts}  total={len(matchups)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
