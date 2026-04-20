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

### Driver Interface
A thin stateless Node.js driver at [driver/driver.js](driver/driver.js) exposes line-delimited JSON-RPC calls over stdin/stdout. Python client at [src/ps_driver.py](src/ps_driver.py) holds the search tree; each call takes a serialized state + args and returns a new state + metadata. No session state persists in the driver.

**1. `start_battle({teams, seed?}) → {state}`** ✓

Builds a fresh `gen5doublescustomgame` battle with normalized `PokemonSet` entries, handles the `teampreview` step automatically, and returns `State.serializeBattle(battle)`.

**2. `execute_turn({state, actions, rng_overrides?}) → {state, rng_events[], choices}`** ✓

Deserializes, applies RNG hooks, runs one turn via `battle.makeChoices(p1Choice, p2Choice)`, reserializes. Actions are a per-side 2-tuple with shapes:
- `{type:"move", move:"Rock Slide"}` (spread — target inferred from move metadata)
- `{type:"move", move:"Sacred Sword", target:"foeSlot0"|"foeSlot1"|"allySlot0"|"allySlot1"}`
- `{type:"switch", to:"Darmanitan"}`
- `{type:"pass"}`

**3. `damage_range({attacker, defender, move}) → {min, max, defenderHp, accuracy, ohko}`** ✓

Builds a throwaway battle with the attacker/defender in slot 0 (filler Ditto in slot 1), forces `battle.randomizer` to roll 0 then roll 15 around calls to `BattleActions.getDamage()`. Skeleton — no spread reduction, no crit variant, no multi-hit handling yet.

**4. `enumerate_turn({state, subway_ai_sides?}) → {requestState, p1_choice_count, p2_choice_count, action_pairs[]}`** ✓

Dispatches on `battle.requestState`:
- **`'move'`**: per-slot candidates come from `pokemon.getMoveRequestData()` (handles PP=0 / Taunt / Disable / Encore / Choice-lock / semi-lock / trapped / Struggle fallback). Single-target moves fan out to each valid target; spread/self/side moves emit no target arg. Per-side = slot0 × slot1 filtered for same-destination switches.
- **`'switch'`** (post-KO mid-turn): for each side, reads `side.activeRequest.forceSwitch[]`. Slots with `forceSwitch[i] === true` enumerate bench switch-ins; other slots emit a single `pass`. Sides with no active request contribute `null` (execute_turn skips them in `makeChoices`). Same duplicate-switch-to filter. **Bench-undersupply:** when both slots must switch but fewer bench mons are alive, one slot switches and the other passes; enumerates all valid assignments (required switches = `min(bench_available, force_slot_count)`).

Pair output is `p1_choices × p2_choices` in both cases.

**`subway_ai_sides`** (optional, list of `"p1"`/`"p2"`): for each listed side, strip voluntary switches from that side's move-phase action space. Battle Subway AI has no ConsiderSwitching flag (see [BattleSubwayAI.md](BattleSubwayAI.md)), so voluntary switches are not part of its real action space — pruning here shrinks the adversarial branching factor. Forced post-KO switches are unaffected. Python callers pass `subway_ai_sides=["p2"]` when enumerating for adversarial scoring.

**`execute_turn` in switch phase:** `actions.p1`/`actions.p2` can be `null` for a side with no pending request; `formatSideChoice` returns an empty string and `battle.makeChoices` skips that side.

**Enumeration caveats (deferred):**
- **Custom legality gates not in `moveSlots.disabled`**: Fake Out / First Impression / Mat Block (turn-1-only after switch-in) — PS enforces these via move `onTry` hooks at execution time, not via the disabled flag. Currently emitted as legal candidates that will fail if chosen.
- **Action-space explosion**: in a full 4v4 doubles with bench, a side can have ~40+ choices; pair product hits ~1600+. Fine for tree search, but the solver will need to prune rather than materialize all pairs up front.

**5. `best_pair_minimax({state, depth, top_k?, subway_ai_sides?}) → {chosen_pair, scored[], depth, cache_entries}`** ✓

Adversarial minimax search run in-process in the driver. `depth ∈ {1, 2}`; depth-2 uses top-K filtering on p1 actions (default `top_k=5`) with the ply-1 adversarial `b1` fixed to the depth-1 worst-case response (approximation — see [src/evaluator.py](src/evaluator.py) module docstring). Switch phases are transparent: each post-KO switch is evaluated by the value of the resulting move-phase, minimaxed, not counted as a ply. Evaluation is p1 HP-fraction sum − p2 HP-fraction sum; terminal states are ±∞.

In-process per-pair cloning (`JSON.stringify(serializeBattle) → parse → deserializeBattle` — string-round-trip avoids aliasing between clones) avoids a Python/IPC round-trip per branch; roughly **2× faster** than the previous Python-side minimax for the same search. Transposition table keyed on `(state_hash, p1_action_hash, p2_action_hash)` lives in the driver; cleared via `clear_minimax_cache`, inspected via `minimax_cache_stats`.

`scored[]` shape: `{p1, d1, d2, adv_p2}` per entry, sorted by `d2` desc (depth-1 sets `d2 = d1`). Infinities are wire-encoded as `"inf"`/`"-inf"` strings (JSON has no native Infinity); Python [src/evaluator.py](src/evaluator.py) decodes them back to `math.inf`. Python callers use `best_pair_minimax` / `best_pair_minimax_depth2` in [src/evaluator.py](src/evaluator.py), which are thin shims around this RPC.

### RNG Interception

PS has no native "kind" tag on its PRNG consultations — all rolls funnel through three untagged primitives on `battle.prng`: `random(n)`, `randomChance(num, denom)`, `sample(arr)` (plus `shuffle`, which calls `random` internally). Kind is implicit in the call site.

**Architecture:** wrap `battle.prng.random/randomChance/sample/shuffle` directly — a single choke point that catches every consultation. For each call, parse `new Error().stack`, skip our own wrappers and PRNG plumbing frames, and map the first remaining PS frame (e.g. `BattleActions.hitStepAccuracy`) to a semantic `kind` via a `KIND_BY_CALLER` table. For catch-all event-dispatch frames (`Battle.onStart`, `Battle.onBeforeMove`, `Battle.onStallMove`, `Battle.onDamagingHit`), a second disambiguation pass reads `battle.effect.id` (the effect currently being dispatched, e.g. `slp`, `confusion`, `par`, `taunt`, `encore`, `disable`, `attract`) to pick the right kind.

**Mapped kinds (observed):**
- Move execution: `accuracy` (`BattleActions.hitStepAccuracy`), `crit` (`BattleActions.getDamage`), `damage_roll` (`Battle.randomizer`), `secondary` (`BattleActions.secondaries`, `BattleActions.selfDrops` — same `random(100) < chance` shape; covers self-boost/drop moves like Draco Meteor / Close Combat), `multi_hit` (`BattleActions.hitStepMoveHitLoop`, `BattleActions.tryMoveHit`).
- Targeting / ordering: `speed_tie` (`Battle.speedSort`), `redirect_target` (`Battle.getTarget`, `Side.randomFoe`), `random_switchable`.
- Status turn rolls (via `Battle.onBeforeMove` + effect id): `fullpara` (par), `confusion_self_hit` (confusion), `sleep_turn` (slp), `freeze_turn` (frz), `attract_immobilize` (attract).
- Duration picks (via `Battle.onStart` + effect id): `sleep_duration` (slp), `confusion_duration` (confusion), `taunt_duration` (taunt), `encore_duration` (encore), `disable_duration` (disable).
- Other event-dispatch frames: `stall` (`Battle.onStallMove` — Protect/Detect decay), `contact_ability` (`Battle.onDamagingHit` — Static/Flame Body/Effect Spore/etc.).

Unknown callers fall through to `kind: "unknown"` with the caller name and effect visible — surfaces new kinds by construction. Coverage verified by [src/probe_rng_kinds.py](src/probe_rng_kinds.py), which stages scenarios for each class and asserts no unknowns remain.

**`rng_events[]` entry shape:**
```json
{
  "kind": "accuracy",
  "caller": "BattleActions.hitStepAccuracy",
  "effect": "",
  "primitive": "randomChance",
  "args": [110, 100],
  "outcome": true,
  "forced": false
}
```
- `primitive` ∈ `random | randomChance | sample | shuffle` — identifies the distribution semantics (randomChance is Bernoulli, random(n) is uniform int [0,n), sample is uniform over array).
- `args` preserves raw primitive args; Python doesn't need PS metadata to understand the distribution.
- `effect` is `battle.effect.id` at call time (empty string if no effect is being dispatched). Disambiguates catch-all event frames; also useful diagnostic signal for other kinds.
- `forced: true` means the outcome came from `rng_overrides`, not the PRNG.

**`rng_overrides[]` entry shape:** `{kind, actor?, move?, force}`. All filters are optional except `kind`. `actor` is `"p1.slot0"`-style (matched against `battle.activePokemon` at call time). `move` is the full-title-case move name (matched against `battle.activeMove.name`).

**Force semantics (currently implemented):**
| kind | force values |
|---|---|
| `damage_roll` | `"min"` (roll 15), `"max"` (roll 0), `0..15` |
| `accuracy` | `"hit"`, `"miss"` |
| `crit` | `"crit"`, `"no_crit"` |
| `secondary` | `"proc"` (roll 0), `"no_proc"` (roll 99), `0..99` |

Deferred: `multi_hit` (needs sample-from-array semantics), `speed_tie` (shuffle forcing), and Bernoulli-style forcing for the remaining classified-but-not-yet-forceable kinds (`fullpara`, `confusion_self_hit`, `sleep_turn`, `freeze_turn`, `attract_immobilize`, `contact_ability`, `stall`) plus duration picks (`sleep_duration`, `confusion_duration`, `taunt_duration`, `encore_duration`, `disable_duration`). Backstop means nothing is silent — unknowns are visible in the event stream and can be mapped iteratively.

### RNG & Tree Exploration
`rng_overrides` is the steering wheel for both modes of the classifier:

- **Tree exploration:** Python runs a turn with empty overrides, inspects `rng_events`, picks a non-degenerate event to branch on, and re-runs with `rng_overrides` pinning the alternate outcome. Recurse. Only branch on events whose `p` has multiple non-zero outcomes.
- **Worst-case collapse:** pin *every* dimension to the pessimistic outcome for our side (all our attacks miss, all opponent's hit, min damage in our favor, max against, opponent flinches, etc.). If the line still wins, it's deterministic — no enumeration needed.

Same mechanism, different purpose. The classifier always runs worst-case collapse first; only non-surviving lines trigger the tree exploration.

### Battle AI
Gen 5 trainer AI is rule-based (flag-driven move scoring) and non-adaptive — given the same board state and AI flags, the opponent picks the same action. The general framework (TRY_TO_FAINT, CHECK_BAD_MOVE, CHECK_VIABILITY, etc.) is partially documented from ROM disassembly work, but **a concrete per-trainer-class flag table for Battle Subway trainers has not been located**. Until we have one (either by sourcing a disassembly reference or reverse-engineering empirically), the classifier uses **adversarial minimax** as the opponent model — pessimistic-safe for classification (upper-bounds risk; real `P(win)` ≥ the minimax estimate). Swap to a scripted policy later if/when the AI spec is obtained — this will sharpen `P(win)` on Category 2 matchups and shrink the game tree.

**Pruning from AI behavior (already applied):** BattleSubwayAI.md documents that the Subway AI has no `ConsiderSwitching` flag — it never voluntarily switches, only forced post-KO switches occur. `enumerate_turn` accepts `subway_ai_sides=["p2"]` to strip p2 voluntary switches from the move-phase action space. This is strictly pessimistic-preserving (we are removing actions the adversary will never take) and shrinks the adversarial branching factor meaningfully.

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
2. **Driver skeleton** ✓ — `start_battle` + `execute_turn` + `damage_range` over stdin/stdout; state round-trip verified across turns
3. **Damage calc wiring** ✓ — `damage_range` via `BattleActions.getDamage()`; validated vs Smogon (Terrakion Sacred Sword vs 252 HP Scrafty: 92–110)
4. **RNG interception** ✓ — PRNG-level hooks emit kinded events; forcing implemented for `damage_roll`, `accuracy`, `crit`, `secondary` with actor/move filters
5. **enumerate_turn** ✓ — legal action enumeration per side; handles `requestState` ∈ (`'move'`, `'switch'`); switch-phase round-trip verified (forced KO → switch enumeration → next turn resumes as `'move'`)
6. **Broader RNG coverage** — classification ✓ via [src/probe_rng_kinds.py](src/probe_rng_kinds.py) (multi-hit, paralysis, sleep, confusion, contact-ability all classified; `Battle.onStart`/`onBeforeMove`/`onStallMove`/`onDamagingHit` disambiguated via `battle.effect.id`). Forcing for the newly-classified kinds (`multi_hit`, `speed_tie`, status turn rolls, duration picks, `contact_ability`, `stall`) still pending — needed before worst-case collapse can cover them.
7. **Matchup classifier** — worst-case-collapse first, tree search for uncertain lines; produces (category, P(win), line) per matchup. *In progress:* HP-fraction evaluator + adversarial depth-2 minimax action selector with top-K filtering, JS-side search via `best_pair_minimax` RPC, Subway-AI pruning on p2, switch-phase rollthrough — driving end-to-end rollouts ([src/demo_rollout.py](src/demo_rollout.py), [src/evaluator.py](src/evaluator.py)). Worst-case-collapse proof path and branching-factor-aware deeper search still pending.
8. **Full enumeration** — all trainer pools × draw combinations × 6 lead configs
9. **Risk report** — win probability per trainer, worst-case matchups, threat Pokémon ranking
10. *(Later)* Lookup table construction via retrograde analysis / minimax over game tree
