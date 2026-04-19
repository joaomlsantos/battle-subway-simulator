# Battle Subway Analyzer

## Goal
Simulate a Pokémon Black/White Battle Subway Super Doubles run (target: 500-win streak) against all possible opponent combinations. The core output is a risk profile: identifying deterministic wins, ordering-constrained wins, RNG-dependent scenarios, and genuine loss scenarios — so the player can prepare optimal lines and understand their real risk exposure.

## The Team
```
Terrakion @ Wide Lens
Ability: Justified
Level: 50
EVs: 252 Atk / 4 SpD / 252 Spe
Jolly Nature
Rock Slide / Sacred Sword / X-Scissor / Protect

Whimsicott @ Focus Sash
Ability: Prankster
Level: 50
EVs: 4 Def / 252 SpA / 252 Spe
Timid Nature
Giga Drain / Beat Up / Tailwind / Sunny Day

Darmanitan @ Life Orb
Ability: Sheer Force
Level: 50
EVs: 252 Atk / 4 SpD / 252 Spe
Adamant Nature
Flare Blitz / Rock Slide / Earthquake / Protect

Thundurus (M) @ Expert Belt
Ability: Prankster
Level: 50
EVs: 4 Def / 252 SpA / 252 Spe
Timid Nature / IVs: 0 Atk
Thunderbolt / Grass Knot / Psychic / Dark Pulse
```

**Primary lead**: Whimsicott + Terrakion. Core gameplan: Prankster Tailwind T1, Beat Up → Justified gives Terrakion +3/+4 Attack, then sweep under Tailwind with Darmanitan + Thundurus as back.

**Key mechanics to get right:**
- Beat Up hit count is based on healthy party members (affects Justified stack count)
- Prankster gives +1 priority to status moves (Tailwind, Sunny Day)
- Sheer Force suppresses secondary effects AND negates Life Orb recoil on affected moves
- Wide Lens: Rock Slide accuracy = 90 × 1.1 = **99%, not 100%** — cannot be treated as guaranteed

## Team Configurations
With 4 mons and no ordering within lead/back pairs, there are **C(4,2) = 6 lead configurations**:
- Terrakion + Whimsicott | Darmanitan + Thundurus ← expected primary
- Terrakion + Darmanitan | Whimsicott + Thundurus
- Terrakion + Thundurus | Whimsicott + Darmanitan
- Whimsicott + Darmanitan | Terrakion + Thundurus
- Whimsicott + Thundurus | Terrakion + Darmanitan
- Darmanitan + Thundurus | Terrakion + Whimsicott

## Opponent Data
- Source: https://bulbapedia.bulbagarden.net/wiki/List_of_Battle_Subway_Trainers
- Relevant pool: Super Doubles trainers (battles 111–300 range)
- Each trainer has a personal Pokémon pool; they draw 4 randomly per battle and lead with 2
- All opponent sets are fully known (nature, EVs, IVs, moves, item, ability)

## Data Structures

Defined in [src/model.py](src/model.py) as plain classes with class-level type annotations (not `@dataclass`). Fields are `camelCase`.

```python
class Course(Enum):
    NORMAL_COURSE = 0
    SUPER_COURSE = 1

class BattleSubwayPokemon:
    name: str                    # species name, full title case e.g. "Garchomp"
    item: str                    # full title case e.g. "Wide Lens"
    nature: str                  # full title case e.g. "Jolly"
    moves: List[str]             # full title case e.g. "Rock Slide"; max 4
    evs: Dict[str, int]          # keys: "hp", "attack", "defense", "sp_atk", "sp_def", "speed"

class BattleSubwayTrainer:
    pokemonList: List[BattleSubwayPokemon]
    name: str
    trainerClass: str
    battleNumRangeMin: int       # index into BATTLE_RANGES (7-battle sets within a course)
    battleNumRangeMax: int       # -1 sentinel means "and beyond" (final tier)
    seventhOpponent: bool
    course: Course
    internalId: int              # from Bulbapedia ordering
    href: str                    # source URL with anchor
```

**Persistence:** `data/trainers.json` — written by `save_trainers()`, loaded by `load_trainers()` in [src/scraper.py](src/scraper.py). `Course` serializes as its name ("NORMAL_COURSE" / "SUPER_COURSE").

**String conventions:** All names stored in full title case with spaces (e.g. `"Rock Slide"`, `"Wide Lens"`, `"Jolly"`). Normalize to PS slugs (`"rockslide"`) only at the boundary where PS/damage-calc integration requires it — never store slugs. Direction: clean → normalized is always safe; reverse requires a lookup table.

## Architecture

### Battle Simulation
**Use Pokémon Showdown as the battle engine via subprocess**, not a custom implementation.

Rationale:
- Gen 5 doubles mechanics are fiddly (Beat Up + Justified, Prankster priority, Sheer Force + Life Orb interaction) — bugs in a custom impl would be hard to catch
- PS has a maintained test suite (`test/sim/`, run with `npx mocha`)
- The subprocess IO API (`./pokemon-showdown simulate-battle`) allows Python to drive it over stdin/stdout without Node bindings
- `sim/state.ts` supports full battle state serialization to JSON — enables checkpointing and branching for the solver

**Key PS source files:**
- `sim/battle-queue.ts` — action queue, priority resolution
- `sim/battle-actions.ts` — move execution, mechanics; `BattleActions.getDamage()` at line 1589 is the standalone damage formula we call directly
- `sim/battle.ts` — turn loop, event system
- `sim/pokemon.ts`, `sim/side.ts`, `sim/field.ts` — state representation; `side.chooseMove(i, target)` / `side.chooseSwitch(slot)` validate choices
- `sim/state.ts` — JSON serialization for checkpointing (`State.serializeBattle` / `State.deserializeBattle`)
- `sim/prng.ts` — RNG (seed for determinism or enumerate branches)
- `Dex.mod('gen5')` — always use this, not default Dex

### TS Driver Interface
A thin stateless TS driver (lives in `pokemon-showdown/` alongside PS source) exposes three calls over stdin/stdout. Python holds the search tree; each TS call takes a serialized state + args and returns a new state + metadata. No session state persists in the driver.

**1. `enumerate_turn(state) → action_pairs[]`**

Returns every legal `(p1_action, p2_action)` pair. Each side's action is a 2-tuple `(slot0, slot1)` since this is Doubles. Per-slot action shapes:
- `{type:"move", move:"Rock Slide", target:"allAdjacentFoes"}` (spread — target fixed by move)
- `{type:"move", move:"Sacred Sword", target:"foeSlot0"|"foeSlot1"}` (single-target, enumerated per legal target)
- `{type:"switch", to:"Darmanitan"}`
- `{type:"pass"}` (slot fainted/forced-empty)

Enumeration filters by legality: Taunt disables status moves, Encore locks in, trapped Pokémon can't switch, etc. PS's `side.chooseMove`/`chooseSwitch` are used as validators per candidate; no "list legal choices" API in PS so we iterate moves × valid targets + switchable team members.

**2. `execute_turn(state, actions, rng_overrides?) → {state, rng_events[]}`**

Executes one turn deterministically. `rng_overrides` is a list of `{kind, actor, move?, force}` entries that intercept PRNG consultations — unset dimensions fall through to PS's PRNG (or a caller-supplied seed). Supported override kinds: `accuracy`, `crit`, `damage_roll`, `secondary`, `multi_hit`, `flinch`, `speed_tie`, `para_block`, `sleep_wake`, `confusion_self_hit`, `freeze_thaw`.

Output `rng_events[]` is an ordered list of **every** RNG consultation PS made, each entry:
```
{kind, actor, move?, p: {outcome: probability, ...}, outcome, forced: bool}
```
Degenerate distributions (e.g. 100%-accuracy moves, `p:{hit:1.0, miss:0.0}`) are still reported so Python doesn't need a replica of PS's move metadata to know which branches exist. `forced: true` indicates the outcome came from `rng_overrides`, not a PRNG roll.

**3. `damage_range(state, attacker, defender, move, options?) → {min, max, ohko, breakdown}`**

Wraps `BattleActions.getDamage()` with deterministic min/max rolls (overrides `randomizer` to return roll 0 and roll 15). Output includes `ohko.guaranteed` (= `min >= defenderHp && accuracy == 1.0`) — the key field for the static determinism check. `options.crit: "both"` returns both no-crit and crit variants.

### RNG & Tree Exploration
`rng_overrides` is the steering wheel for both modes of the classifier:

- **Tree exploration:** Python runs a turn with empty overrides, inspects `rng_events`, picks a non-degenerate event to branch on, and re-runs with `rng_overrides` pinning the alternate outcome. Recurse. Only branch on events whose `p` has multiple non-zero outcomes.
- **Worst-case collapse:** pin *every* dimension to the pessimistic outcome for our side (all our attacks miss, all opponent's hit, min damage in our favor, max against, opponent flinches, etc.). If the line still wins, it's deterministic — no enumeration needed.

Same mechanism, different purpose. The classifier always runs worst-case collapse first; only non-surviving lines trigger the tree exploration.

### Battle AI
Battle Subway uses a **scripted, non-adaptive AI** — behavior is documented and deterministic given a board state. No learned opponent model needed.

### Matchup Classification
Every matchup scenario is classified into one of:
1. **Deterministic win** — proven optimal line wins regardless of all RNG outcomes (P(win) = 1.0). Verified once, archived.
2. **RNG-dependent win** — winnable under optimal play but some branches depend on accuracy/crit/proc rolls. Quantify P(win) exactly via tree search.
3. **Risky / ambiguous** — optimal play does not guarantee a win, or correct line is non-obvious. Flag for deep analysis.

**Important:** "deterministic" is **not** "we picked a representative roll and it worked." Cumulative 1% misses compound — 20 turns at 99% each = 82% overall. The classifier must check that the line wins on *every* reachable RNG branch (or equivalently, under worst-case collapse — see TS Driver Interface above).

**Classification flow per line:**
1. **Worst-case collapse:** run `execute_turn` with `rng_overrides` pinning every dimension pessimistically. If still winning → Category 1, archive and done.
2. **Static shortcut for single turns:** `damage_range(...).ohko.guaranteed == true` and speed is unambiguous → same conclusion without running the turn.
3. **Tree search:** if worst-case loses, enumerate branches from non-degenerate `rng_events`, multiply probabilities along win paths, sum → P(win). Assign Category 2 (with probability) or Category 3 (if P(win) below threshold or optimal line unclear).

**Lookup table goal:** precompute `(team_state, opponent_state, board_state) → optimal_action` for all reachable states per matchup. Matchups resolving to a single path from root = deterministic wins. Enables zero-overhead decision making.

### Key Risk Factors to Map
- **Trick Room** — highest priority threat class; this team is entirely speed-dependent
- **Fake Out** — on Terrakion before Beat Up disrupts the engine
- **Rock Slide miss (1%)** — any scenario requiring Rock Slide to hit cannot be deterministic
- **Rock Slide flinch dependency** — scenarios that are only "comfortable" wins with a flinch

## Phased Roadmap
1. **Data** ✓ — scraper + validator; 300 trainers in `data/trainers.json`, clean against PS Gen 5 dex
2. **TS driver skeleton** — stub out `enumerate_turn` / `execute_turn` / `damage_range` over stdin/stdout; validate round-trip serialization against a known state
3. **Damage calc wiring** — `damage_range` backed by `BattleActions.getDamage()`; validate against known Gen 5 damage outputs from PS's test suite
4. **Battle simulator PoC** — `execute_turn` with `rng_overrides` support; validate against ~10 representative trainer scenarios end-to-end
5. **Matchup classifier** — worst-case-collapse first, tree search for uncertain lines; produces (category, P(win), line) per matchup
6. **Full enumeration** — all trainer pools × draw combinations × 6 lead configs
7. **Risk report** — win probability per trainer, worst-case matchups, threat Pokémon ranking
8. *(Later)* Lookup table construction via retrograde analysis / minimax over game tree
