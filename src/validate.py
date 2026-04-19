"""Validate scraped trainer data against Pokémon Showdown's dex.

Checks that every species, item, and move in data/trainers.json resolves
to a PS slug, plus sanity checks on move count, nature, and EV totals.
"""
import os
import re
from collections import defaultdict
from typing import Dict, Set

from scraper import load_trainers


PS_DATA = os.path.join(os.path.dirname(__file__), "..", "pokemon-showdown", "data")

NATURES = {
    "Hardy", "Lonely", "Brave", "Adamant", "Naughty",
    "Bold", "Docile", "Relaxed", "Impish", "Lax",
    "Timid", "Hasty", "Serious", "Jolly", "Naive",
    "Modest", "Mild", "Quiet", "Bashful", "Rash",
    "Calm", "Gentle", "Sassy", "Careful", "Quirky",
}


def to_id(s: str) -> str:
    """PS toID: lowercase + strip anything non-alphanumeric."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def extract_top_level_keys(ts_path: str) -> Set[str]:
    """Grab the top-level object keys from a PS data TS file.

    Entries look like `\tfoo: {` or `\t"foo": {` at indent level 1.
    """
    with open(ts_path, encoding="utf-8") as f:
        content = f.read()
    matches = re.findall(r'^\t(?:"([^"]+)"|([A-Za-z0-9_]+)): \{', content, re.MULTILINE)
    return {quoted or bare for quoted, bare in matches}


def load_ps_slugs() -> Dict[str, Set[str]]:
    return {
        "species": extract_top_level_keys(os.path.join(PS_DATA, "pokedex.ts")),
        "moves": extract_top_level_keys(os.path.join(PS_DATA, "moves.ts")),
        "items": extract_top_level_keys(os.path.join(PS_DATA, "items.ts")),
    }


def validate(trainers, ps: Dict[str, Set[str]]) -> Dict[str, list]:
    issues = defaultdict(list)

    for t in trainers:
        ctx = f"[{t.internalId:03}] {t.trainerClass} {t.name}"
        for i, p in enumerate(t.pokemonList):
            pctx = f"{ctx} mon#{i} ({p.name!r})"

            if not p.name:
                issues["empty_species"].append(pctx)
            elif to_id(p.name) not in ps["species"]:
                issues["unknown_species"].append(f"{pctx}: {p.name!r}")

            if p.item and to_id(p.item) not in ps["items"]:
                issues["unknown_item"].append(f"{pctx}: {p.item!r}")

            if not (1 <= len(p.moves) <= 4):
                issues["bad_move_count"].append(f"{pctx}: {len(p.moves)} moves")

            for m in p.moves:
                if not m:
                    issues["empty_move"].append(pctx)
                elif to_id(m) not in ps["moves"]:
                    issues["unknown_move"].append(f"{pctx}: {m!r}")

            if p.nature and p.nature not in NATURES:
                issues["unknown_nature"].append(f"{pctx}: {p.nature!r}")

            ev_total = sum(p.evs.values())
            if ev_total > 510:
                issues["ev_over_510"].append(f"{pctx}: total={ev_total}")

    return issues


def main() -> int:
    trainers = load_trainers()
    ps = load_ps_slugs()
    total_mons = sum(len(t.pokemonList) for t in trainers)

    print(f"Loaded {len(trainers)} trainers ({total_mons} mon entries)")
    print(f"PS dex: {len(ps['species'])} species, {len(ps['moves'])} moves, {len(ps['items'])} items")
    print()

    issues = validate(trainers, ps)

    if not issues:
        print("OK - no issues found")
        return 0

    for category, items in sorted(issues.items()):
        print(f"[{category}] {len(items)} issue(s)")
        for item in items[:10]:
            print(f"  {item}")
        if len(items) > 10:
            print(f"  ... and {len(items) - 10} more")
        print()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
