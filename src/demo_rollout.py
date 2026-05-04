"""Linear battle rollout demo against a fixed hardcoded opponent team.

See [src/demo_trainer_rollout.py] for the trainer-data-driven variant. The
shared rollout loop lives in [src/rollout.py].
"""
from ps_driver import PSDriver
from rollout import run_rollout


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

# Sample opponent team — picked arbitrarily for the demo; not tuned for realism.
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

P1_TEAM = [TERRAKION, WHIMSICOTT, DARMANITAN, THUNDURUS]
P2_TEAM = [GARCHOMP, LATIOS, METAGROSS, HYDREIGON]


def main():
    with PSDriver() as drv:
        result = run_rollout(drv, P1_TEAM, P2_TEAM)
    return 0 if result["ended"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
