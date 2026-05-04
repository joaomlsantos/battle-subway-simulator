"""Adapt scraped BattleSubwayPokemon entries (data/trainers.json) into the
PS set shape consumed by the driver.

Battle Subway opponents use regular abilities only (never hidden). When a
species has two non-hidden abilities (slots 0 and 1) the game can roll either,
so we fan out into one PS set per ability — each ability is treated as a
distinct Pokémon for classification purposes.

EV key translation (trainers.json → PS):
    attack  → atk     defense → def     sp_atk → spa
    sp_def  → spd     speed   → spe     hp     → hp
"""
import json
import os
from typing import Dict, List

_ABILITIES_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "abilities.json")

_EV_KEY_MAP = {
    "hp": "hp",
    "attack": "atk",
    "defense": "def",
    "sp_atk": "spa",
    "sp_def": "spd",
    "speed": "spe",
}


def load_abilities(path: str = _ABILITIES_PATH) -> Dict[str, Dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def regular_abilities(abilities_table: Dict[str, Dict], species: str) -> List[str]:
    """Return the regular (non-hidden) ability slots for a species, in slot
    order. Slot 0 first, then slot 1 if defined. Hidden ('H') and Signature
    ('S') are excluded — Battle Subway never rolls those.
    """
    entry = abilities_table.get(species)
    if entry is None:
        raise KeyError(f"species {species!r} not in abilities table")
    abilities = entry.get("abilities", {})
    out = []
    if "0" in abilities:
        out.append(abilities["0"])
    if "1" in abilities:
        out.append(abilities["1"])
    if not out:
        raise ValueError(f"species {species!r} has no regular abilities")
    return out


def _translate_evs(evs: Dict[str, int]) -> Dict[str, int]:
    return {_EV_KEY_MAP[k]: v for k, v in (evs or {}).items() if k in _EV_KEY_MAP and v}


def adapt_pokemon(bs_mon: Dict, abilities_table: Dict[str, Dict]) -> List[Dict]:
    """Translate a single scraped Pokemon dict into one PS set per regular
    ability. Returns a list (length 1 or 2) of PS-shaped dicts.
    """
    species = bs_mon["name"]
    base = {
        "species": species,
        "item": bs_mon.get("item", ""),
        "moves": list(bs_mon.get("moves", []))[:4],
        "nature": bs_mon.get("nature", "Serious"),
        "evs": _translate_evs(bs_mon.get("evs", {})),
    }
    return [{**base, "ability": ability} for ability in regular_abilities(abilities_table, species)]


def adapt_pokemon_list(bs_mons: List[Dict], abilities_table: Dict[str, Dict]) -> List[List[Dict]]:
    """Apply adapt_pokemon to each entry. Returns a list-of-lists: outer index
    matches the input order, inner list contains one set per ability variant.
    """
    return [adapt_pokemon(m, abilities_table) for m in bs_mons]
