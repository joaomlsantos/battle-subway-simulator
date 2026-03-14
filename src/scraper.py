import os

from bs4 import BeautifulSoup
import urllib
import urllib.request
import urllib.parse
from typing import List, Optional
from model import BattleSubwayTrainer, BattleSubwayPokemon, Course
from page_caching import url_to_filename, CACHE_DIR


BASE_URL = "https://bulbapedia.bulbagarden.net"

BATTLE_RANGES = [
    (1, 7),
    (8, 14),
    (15, 21),
    (22, 28),
    (29, -1),
]



def load_html(url: str) -> bytes:
    filepath = os.path.join(CACHE_DIR, url_to_filename(url))
    with open(filepath, "rb") as f:
        return f.read()


EV_STATS = ["hp", "attack", "defense", "sp_atk", "sp_def", "speed"]


def parse_trainer_row(row, course: Course) -> Optional[BattleSubwayTrainer]:
    cells = row.find_all("td")
    if len(cells) < 7:
        return None

    try:
        internal_id = int(cells[0].get_text(strip=True))
    except ValueError:
        return None

    # strip symbols for href
    trainer_class = cells[1].get_text(strip=True).translate(str.maketrans("", "", "♂♀*")).strip()
    
    name_cell = cells[2]
    name_link = name_cell.find("a")
    name = name_cell.get_text(strip=True)
    href = BASE_URL + name_link["href"] if name_link and name_link.get("href") else None

    battle_cols = cells[3:8]
    active_ranges = []
    seventh_opponent = False

    for i, cell in enumerate(battle_cols):
        text = cell.get_text(strip=True)
        if "✓" in text or "✔" in text:
            active_ranges.append(i)
            style = cell.get("style", "") + cell.get("bgcolor", "")
            if "e7c46e" in style.lower():
                seventh_opponent = True

    if not active_ranges:
        return None

    battle_min = BATTLE_RANGES[active_ranges[0]][0]
    battle_max = BATTLE_RANGES[active_ranges[-1]][1]

    trainer = BattleSubwayTrainer()
    trainer.internalId = internal_id
    trainer.trainerClass = trainer_class
    trainer.name = name
    trainer.pokemonList = []
    trainer.battleNumRangeMin = battle_min
    trainer.battleNumRangeMax = battle_max
    trainer.seventhOpponent = seventh_opponent
    trainer.course = course
    trainer.href = href

    return trainer


def parse_pokemon_from_href(href: str) -> List[BattleSubwayPokemon]:

    if "#" in href:
        url, anchor = href.split("#", 1)
    else:
        url, anchor = href, None

    #req = urllib.request.Request(href, headers={"User-Agent": "Mozilla/5.0"})
    #html = urllib.request.urlopen(req).read()
    html = load_html(url)
    soup = BeautifulSoup(html, "html.parser")

    if anchor:
        heading_span = soup.find("span", {"id": anchor})
        if not heading_span:
            return []
        parent = heading_span.find_parent(["h2", "h3", "p"])
        if not parent:
            return []
        table = parent.find_next_sibling("table")
    else:
        table = soup.find("table")

    if not table:
        return []

    pokemon_list = []

    for row in table.find_all("tr")[2:]:  # skip two header rows
        cells = row.find_all("td")
        if len(cells) < 14:
            continue

        name_cell = cells[2]
        name = name_cell.get_text(strip=True)

        item_cell = cells[3]
        item = item_cell.get_text(strip=True)

        moves = [cells[i].get_text(strip=True) for i in range(4, 8)]

        nature = cells[8].get_text(strip=True)

        evs = {}
        for i, stat in enumerate(EV_STATS):
            raw = cells[9 + i].get_text(strip=True)
            evs[stat] = int(raw) if raw != "-" else 0

        pokemon = BattleSubwayPokemon()
        pokemon.name = name
        pokemon.item = item
        pokemon.moves = moves
        pokemon.nature = nature
        pokemon.evs = evs

        pokemon_list.append(pokemon)

    return pokemon_list


def run_parser(page_url: str) -> List[BattleSubwayTrainer]:
    #req = urllib.request.Request(page_url, headers={"User-Agent": "Mozilla/5.0"})
    #html = urllib.request.urlopen(req).read()
    html = load_html(page_url)
    soup = BeautifulSoup(html, "html.parser")

    trainers: List[BattleSubwayTrainer] = []

    section_map = {
        "Normal_Course_Trainers": Course.NORMAL_COURSE,
        "Super_Course_Trainers": Course.SUPER_COURSE,
    }

    for section_id, course in section_map.items():
        heading = soup.find("span", {"id": section_id})
        if not heading:
            continue

        table = heading.find_parent("h2").find_next_sibling("table")
        if not table:
            continue

        for row in table.find_all("tr")[2:]:  # skip the two header rows
            trainer = parse_trainer_row(row, course)
            if trainer:
                print(trainer.href)
                trainer.pokemonList = parse_pokemon_from_href(trainer.href)
                trainers.append(trainer)

    return trainers


if __name__ == "__main__":
    bulbapedia_page = "https://bulbapedia.bulbagarden.net/wiki/List_of_Battle_Subway_Trainers"
    trainers = run_parser(bulbapedia_page)

    print(f"Parsed {len(trainers)} trainers")
    for t in trainers[:5]:
        battle_range_str = f"{t.battleNumRangeMin}+" if t.battleNumRangeMax == -1 else f"{t.battleNumRangeMin}-{t.battleNumRangeMax}"
        print(f"\n  [{t.internalId:03}] {t.trainerClass} {t.name} | "
              f"battles {battle_range_str} | "
              f"course={t.course.name} | 7th={t.seventhOpponent}")
        
        for p in t.pokemonList[:3]:
            ev_str = ", ".join(f"{stat}:{val}" for stat, val in p.evs.items() if val > 0)
            print(f"    - {p.name} @ {p.item} | {p.nature} | [{', '.join(p.moves)}] | EVs: {ev_str}")