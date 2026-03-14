

from enum import Enum
from typing import Dict, List


class Course(Enum):
    NORMAL_COURSE = 0
    SUPER_COURSE = 1

class BattleSubwayPokemon:
    name: str
    item: str
    nature: str
    moves: List[str]
    evs: Dict[str, int]


class BattleSubwayTrainer:
    pokemonList: List[BattleSubwayPokemon]
    name: str
    trainerClass: str
    battleNumRangeMin: int
    battleNumRangeMax: int
    seventhOpponent: bool
    course: Course
    internalId: int     # storing this according to bulbapedia, not sure if it will actually be useful
    href: str



