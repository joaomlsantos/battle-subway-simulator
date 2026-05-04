# Battle Subway Simulator

A search-based decision engine for the Pokémon Black/White Battle Subway. The goal is a complete risk profile for a target 500-win Super Doubles streak: classify every reachable opponent matchup as a deterministic win, an RNG-dependent win (with exact P(win)), or a genuine loss scenario — and produce optimal-play lookup tables for live use.

The team being analyzed is fixed (Terrakion / Whimsicott / Darmanitan / Thundurus — see [CLAUDE.md](CLAUDE.md) for the full set). The opponent pool is the Battle Subway Super Doubles trainer roster (battles 111–300), scraped from Bulbapedia into [data/trainers.json](data/trainers.json).

## Architecture in one paragraph

Pokémon Showdown is the battle engine — driven as a Node subprocess via line-delimited JSON-RPC ([driver/driver.js](driver/driver.js), Python client at [src/ps_driver.py](src/ps_driver.py)). On top of PS sit a static depth-1 evaluator with damage matrix + board snapshot ([driver/static_eval.js](driver/static_eval.js)) for fast pair scoring, an adversarial minimax search with top-K depth-2 expansion and `d2_worst` / `d2_expected` extended scoring, RNG-kind classification + forcing for worst-case collapse, and a trainer-data ability fan-out at the boundary ([src/trainer_adapter.py](src/trainer_adapter.py)). Read [CLAUDE.md](CLAUDE.md) for full mechanics — this README is just the user-facing entry points.

## Setup

Requires Python ≥ 3.9, Node.js (for the bundled Pokémon Showdown submodule), and [`uv`](https://github.com/astral-sh/uv) for Python dependency management.

```bash
# Install Python dependencies
uv sync

# Build the abilities lookup table (one-off; required before any rollout)
node tools/build_abilities.js

# Verify the PS submodule builds cleanly
cd pokemon-showdown && npm install && cd ..
```

The PS submodule is checked in under `pokemon-showdown/` and is what the driver spawns. No global PS install is needed.

## Common commands

All Python entry points are run with `uv run python src/<file>.py`. Logs are written to `logs/`.

### Single-matchup demo rollout (against a hand-rolled P2 team)

```bash
uv run python src/demo_rollout.py
```

Runs the fixed P1 team against a hard-coded P2 team defined inside the script. Useful for smoke-testing evaluator changes — outputs the full top-K scored table per phase plus the chosen action.

### Single-matchup rollout against a real Subway trainer

```bash
# List available trainers
uv run python src/demo_trainer_rollout.py --list-trainers --course SUPER_COURSE

# Run a specific matchup
uv run python src/demo_trainer_rollout.py --trainer-id 165
uv run python src/demo_trainer_rollout.py --trainer-id 165 --draw 0,1,2,3 --lead 0,1
uv run python src/demo_trainer_rollout.py --trainer-id 165 --abilities 1,0,0,0
```

Picks 4 of the trainer's pool (`--draw`), the lead pair (`--lead`), and one ability variant per drawn mon (`--abilities`, indexes into the species' regular abilities), then runs the same rollout loop with `assume_hit` mode on by default. `--seed a,b,c,d` reproduces a saved RNG seed for repeatable runs.

### Full enumeration over a trainer

```bash
uv run python src/enum_matchups.py --trainer-id 165
uv run python src/enum_matchups.py --trainer-id 165 --p1-leads all     # all 6 P1 lead configs
uv run python src/enum_matchups.py --trainer-id 165 --limit 20         # smoke-test cap
```

Enumerates every (P1 lead × P2 draw × P2 lead × P2 ability) combination and writes one row per matchup to `data/matchups/trainer-{id}-{stamp}.json`, with atomic rewrite per battle so a crash leaves a valid file. Pool size drives cost: SUPER_COURSE pools range from 7 to 88, so for large pools use `--limit` or pick a smaller trainer for exhaustive runs.

### Live-play assist (single-turn decision)

```bash
# Full-info gospel mode
uv run python src/live_turn.py --scenario scenario.json
cat scenario.json | uv run python src/live_turn.py

# Trainer-aware observation filter
uv run python src/live_turn.py --list-consistent --trainer-id 165 \
    --revealed-leads "Audino,Chansey"
uv run python src/live_turn.py --list-consistent --trainer-id 165 \
    --revealed-leads "Audino,Chansey" --show-config 1
```

Reads a scenario JSON describing an observed mid-battle state, mutates a fresh PS battle to match, and runs one depth-2 minimax pass. See the docstring at the top of [src/live_turn.py](src/live_turn.py) for the full scenario shape — supported overrides include `hp_frac` / `status` / `boosts` / `stall_counter` / `fainted` per slot, weather, Trick Room, Tailwind, Reflect, Light Screen, and a switch-phase mode for post-KO replacement queries.

### RNG-kind probe

```bash
uv run python src/probe_rng_kinds.py
```

Stages scenarios for every classified RNG kind and asserts that no `unknown` callers remain. Run this after touching the RNG hook plumbing in [driver/driver.js](driver/driver.js).

## File layout

```
driver/                   Node JSON-RPC battle driver
  driver.js               start_battle / execute_turn / damage_range / enumerate_turn / best_pair_minimax
  static_eval.js          DamageMatrix + BoardSnap + staticScorePair
src/                      Python client + analysis layer
  ps_driver.py            Subprocess client for driver.js
  evaluator.py            Thin shim around best_pair_minimax RPCs
  rollout.py              Shared per-battle rollout loop with logging
  demo_rollout.py         Demo: fixed P1 vs hand-rolled P2
  demo_trainer_rollout.py Demo: fixed P1 vs a Subway trainer's pool
  enum_matchups.py        Exhaustive sweep over a trainer's matchup space
  live_turn.py            Single-turn decision endpoint for live play
  trainer_adapter.py      Fans BattleSubwayPokemon → PS sets per regular ability
  scraper.py              Bulbapedia scraper + trainers.json IO
  model.py                Domain types (BattleSubwayTrainer / Pokemon, Course)
  probe_rng_kinds.py      RNG-kind classification coverage check
data/
  trainers.json           300 trainers scraped from Bulbapedia
  abilities.json          Gen 5 species → regular abilities (built by tools/build_abilities.js)
  matchups/               Per-trainer enumeration outputs
tools/
  build_abilities.js      Dumps Dex.mod('gen5').species.all() abilities
pokemon-showdown/         PS submodule — battle engine source of truth
logs/                     Per-rollout battle logs
ram_watch/                Live-memory scaffolding for real-time observation (in progress)
```

## Status

See the **Phased Roadmap** section in [CLAUDE.md](CLAUDE.md) for the full picture. Current focus: matchup classifier (item 7) — extending the per-candidate Cat-1 proof signal into a matchup-level archive of deterministic wins. Gating piece is RNG forcing for the remaining classified kinds (`multi_hit`, `speed_tie`, status-turn rolls, duration picks).
