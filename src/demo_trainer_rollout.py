"""Trainer-data-driven rollout demo.

Pick a trainer from data/trainers.json, pick 4 of their Pokémon (a draw),
pick a lead pair, pick one ability variant per drawn mon, and run our fixed
team against them using the shared rollout loop.

Usage:
    python src/demo_trainer_rollout.py --trainer-id 150
    python src/demo_trainer_rollout.py --trainer-id 150 --draw 0,1,2,3 --lead 0,1
    python src/demo_trainer_rollout.py --trainer-id 150 --abilities 1,0,0,0

    # Inspect available trainers:
    python src/demo_trainer_rollout.py --list-trainers --course SUPER_COURSE
"""
import argparse
from typing import List

from model import BattleSubwayTrainer, Course
from ps_driver import PSDriver
from rollout import run_rollout
from scraper import load_trainers
from trainer_adapter import adapt_pokemon, load_abilities


# Our fixed team (from CLAUDE.md). Lead = Terrakion + Whimsicott.
TERRAKION = {
    "species": "Terrakion", "item": "Wide Lens", "ability": "Justified",
    "moves": ["Rock Slide", "Sacred Sword", "X-Scissor", "Protect"],
    "nature": "Jolly", "evs": {"atk": 252, "spd": 4, "spe": 252},
}
WHIMSICOTT = {
    "species": "Whimsicott", "item": "Focus Sash", "ability": "Prankster",
    "moves": ["Energy Ball", "Beat Up", "Tailwind", "Sunny Day"],
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
P1_TEAM = [TERRAKION, WHIMSICOTT, DARMANITAN, THUNDURUS]


def _parse_int_csv(s: str) -> List[int]:
    return [int(x) for x in s.split(",") if x.strip()]


def find_trainer(trainers: List[BattleSubwayTrainer], trainer_id: int) -> BattleSubwayTrainer:
    for t in trainers:
        if t.internalId == trainer_id:
            return t
    raise SystemExit(f"no trainer with internalId={trainer_id}")


def build_opponent_team(trainer, draw_idxs, lead_idxs, ability_idxs, abilities_table):
    """Build 4 PS sets from the trainer's pool.

    draw_idxs: 4 indices into trainer.pokemonList (the mons drawn this battle).
    lead_idxs: 2 indices into draw_idxs (which two lead). Order = slot 0 then slot 1.
    ability_idxs: 4 indices, one per draw slot, selecting 0 or 1 from the mon's
        regular abilities list (from trainer_adapter.regular_abilities).
    """
    if len(draw_idxs) != 4:
        raise SystemExit(f"--draw must have 4 indices, got {len(draw_idxs)}")
    if len(lead_idxs) != 2:
        raise SystemExit(f"--lead must have 2 indices, got {len(lead_idxs)}")
    if any(i < 0 or i >= 4 for i in lead_idxs):
        raise SystemExit(f"--lead indices must be in [0,3] (positions within --draw)")
    if len(ability_idxs) != 4:
        raise SystemExit(f"--abilities must have 4 entries, got {len(ability_idxs)}")

    # Ordered: lead[0], lead[1], then the other two draws in draw-index order.
    ordered_positions = list(lead_idxs) + [i for i in range(4) if i not in lead_idxs]

    ps_team = []
    for pos in ordered_positions:
        pool_idx = draw_idxs[pos]
        if pool_idx < 0 or pool_idx >= len(trainer.pokemonList):
            raise SystemExit(
                f"--draw index {pool_idx} out of range for trainer's {len(trainer.pokemonList)}-mon pool"
            )
        bs_mon = trainer.pokemonList[pool_idx]
        raw = {
            "name": bs_mon.name, "item": bs_mon.item, "nature": bs_mon.nature,
            "moves": bs_mon.moves, "evs": bs_mon.evs,
        }
        variants = adapt_pokemon(raw, abilities_table)
        ab_idx = ability_idxs[pos]
        if ab_idx < 0 or ab_idx >= len(variants):
            raise SystemExit(
                f"ability index {ab_idx} out of range for {bs_mon.name} "
                f"(has {len(variants)} regular abilit{'y' if len(variants) == 1 else 'ies'})"
            )
        ps_team.append(variants[ab_idx])
    return ps_team


def list_trainers(trainers, course_filter=None):
    for t in trainers:
        if course_filter and t.course.name != course_filter:
            continue
        pool = len(t.pokemonList)
        preview = ", ".join(m.name for m in t.pokemonList[:4])
        if pool > 4:
            preview += f", …+{pool - 4}"
        rng = f"{t.battleNumRangeMin}-{t.battleNumRangeMax if t.battleNumRangeMax >= 0 else '+'}"
        print(f"  #{t.internalId:>3} {t.trainerClass:<20} {t.name:<20} "
              f"[{t.course.name:<13} {rng}] pool={pool:>2} : {preview}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trainer-id", type=int, help="internalId from data/trainers.json")
    parser.add_argument("--draw", default="0,1,2,3",
                        help="comma-separated indices into trainer.pokemonList (default first 4)")
    parser.add_argument("--lead", default="0,1",
                        help="comma-separated indices into --draw for slot 0 and slot 1 (default 0,1)")
    parser.add_argument("--abilities", default="0,0,0,0",
                        help="comma-separated ability-slot indices per --draw slot (0 or 1; default all 0)")
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--no-assume-hit", action="store_true",
                        help="disable the 'all moves hit' rollout mode")
    parser.add_argument("--seed", default=None,
                        help="PS PRNG seed as 4 comma-separated u16s (e.g. '1234,5678,9012,3456'); "
                             "default uses driver's [1,2,3,4] — use this to reproduce a saved enum_matchups row")
    parser.add_argument("--list-trainers", action="store_true",
                        help="list available trainers and exit")
    parser.add_argument("--course", choices=["NORMAL_COURSE", "SUPER_COURSE"],
                        help="filter --list-trainers output by course")
    args = parser.parse_args()

    trainers = load_trainers()

    if args.list_trainers:
        list_trainers(trainers, args.course)
        return 0

    if args.trainer_id is None:
        parser.error("--trainer-id is required (or pass --list-trainers)")

    trainer = find_trainer(trainers, args.trainer_id)
    abilities_table = load_abilities()

    p2_team = build_opponent_team(
        trainer,
        draw_idxs=_parse_int_csv(args.draw),
        lead_idxs=_parse_int_csv(args.lead),
        ability_idxs=_parse_int_csv(args.abilities),
        abilities_table=abilities_table,
    )

    header_lines = [
        f"Trainer: #{trainer.internalId} {trainer.trainerClass} {trainer.name} "
        f"({trainer.course.name})",
    ]
    for mon in p2_team:
        header_lines.append(
            f"  - {mon['species']} @ {mon['item']} | {mon['ability']} | {mon['nature']} | "
            f"{'/'.join(mon['moves'])}"
        )

    seed = _parse_int_csv(args.seed) if args.seed else None
    if seed is not None and len(seed) != 4:
        parser.error(f"--seed must be 4 ints, got {len(seed)}")

    draw = "".join(str(i) for i in _parse_int_csv(args.draw))
    lead = "".join(str(i) for i in _parse_int_csv(args.lead))
    abs_ = "".join(str(i) for i in _parse_int_csv(args.abilities))
    log_label = f"t{trainer.internalId}_d{draw}_l{lead}_a{abs_}"

    with PSDriver() as drv:
        result = run_rollout(
            drv, P1_TEAM, p2_team,
            header_lines=header_lines,
            top_k=args.top_k,
            assume_hit=not args.no_assume_hit,
            max_turns=args.max_turns,
            seed=seed,
            log_label=log_label,
        )
    return 0 if result["ended"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
