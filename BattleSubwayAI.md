Battle Subway AI Behavior
Information Model

The AI has perfect knowledge of your Pokémon's species, ability, and moves from the moment they are sent out
The AI has perfect knowledge of your Pokémon's item from the moment they are sent out (debated in the Smogon thread but the prevailing view is yes)
Exception: Illusion (Zoroark) fools the AI — it acts on the disguise, not the true species
IVs are all 31 from battle 21 onward on the Super line; EVs are always fully invested (510), split evenly across 2 or 3 stats

Move Selection

Move selection is score-based: each move gets a base score modified by a series of flag checks (CHECK_BAD_MOVE, CHECK_VIABILITY, TRY_TO_FAINT). Highest score wins
The AI evaluates moves against the current board state only — no lookahead, no modeling of future turns
TRY_TO_FAINT means it will strongly prefer moves that can KO a target this turn
CHECK_BAD_MOVE penalizes moves with no effect (e.g. using a Normal move against a Ghost)
CHECK_VIABILITY scores moves by type effectiveness and expected damage
The AI almost never voluntarily switches. No ConsiderSwitching flag observed for Subway trainers — the great majority of turns only trigger switches when forced by a KO. Four documented exceptions (all considered negligible for v1 classification but flagged here for later refinement):
  1. **Perish Song Count == 1** — will always switch if able.
  2. **Natural Cure holder is statused** — may switch to heal (more common for sleep/freeze, less for poison); probabilistic, not guaranteed.
  3. **Switching to an immunity** — rare; sometimes triggered even after the threatening attacker has already fainted.
  4. **Active has no damaging moves that can harm the player's active**, provided the mon has damaging moves at all. Also covers being Choice-locked into a non-damaging move or into one the target is immune to
The AI treats battles as successive 1v1s rather than holistically — it focuses on knocking out the current target rather than positioning for future turns

Doubles-Specific Behavior

The AI_SCRIPT_DOUBLE_BATTLE flag is active — the AI will not target its own partner with moves that would hit it (e.g. avoids Earthquake if partner is grounded)
Partner awareness extends to the bench: even with one mon fainted, the AI checks whether a move would hit the remaining partner before using it
The AI does model partner type when deciding spread moves — e.g. will avoid Earthquake if its own remaining partner is Ground-weak
The AI is demonstrably worse than Gen 3/4 Frontier AI — it will sometimes target a Pokémon with an ineffective move and continue doing so for multiple turns
The AI appears to favor targeting Pokémon that are in KO range or heavily boosted, sometimes to a fault (can be exploited by baiting with a threatened mon while the real threat sets up)

Targeting

In doubles, single-target move selection appears to follow a greedy highest-damage heuristic against the current active targets
No evidence of the AI predicting switches or playing around future board states

Implications for State Space

Voluntary switches can be pruned from p2's action space entirely during the move phase — only forced post-KO switches need to be enumerated
Dominated action pruning is safe: dominance can be defined purely on immediate board impact (damage + status + debuff) with no need to model bench positioning
The AI's move scoring is deterministic given a board state and AI flags — if the flag table for Subway trainers is confirmed, p2's action is not a branching variable at all, it's a lookup. This would collapse the adversarial dimension entirely for move-phase turns
The main remaining uncertainty is the exact flag table per trainer class — until confirmed via ROM data or emulator memory watching, adversarial minimax remains the safe fallback