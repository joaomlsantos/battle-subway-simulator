"""Heuristic state evaluator and adversarial minimax action selector.

Scores a state from p1's perspective: positive = good for p1. Used by the
classifier to rank our actions via adversarial minimax (pessimistic over p2's
choice) and, later, to score intermediate nodes when tree search hits a depth
cap.

The search itself runs inside the Node driver (see `best_pair_minimax` in
driver/driver.js) — in-process per-pair cloning avoids a Python/IPC round-trip
per branch. This module is a thin shim exposing the same API the classifier
was built against. Shape-for-shape compatible with the previous Python
implementation so `demo_rollout` and downstream callers are unchanged.

v1 metric: sum of HP fractions per side, p1 minus p2. Fainted mons contribute
0, so this captures KO count implicitly without a separate term. Terminal
states override to +/-inf (battle ended with a winner).

Adversarial assumption: we treat p2 as seeing our action and best-responding.
Simultaneous-move value is >= this (Nash eq. >= pure minimax), so this is a
pessimistic-safe lower bound for classification.

Depth-2 uses a top-K filter on p1 actions at ply 1 to keep cost bounded. For
each selected a1, the ply-1 adversarial b1 is *fixed* to the depth-1 worst-case
response (not re-minimaxed with depth-2 lookahead) — a known approximation
that may underestimate the true depth-2 worst case if the adversary's best
ply-1 response shifts under ply-2 awareness.

"Depth" counts **move-phase plies**, not execute_turn calls. Forced switch
phases (post-KO) are rolled through — each switch option is evaluated by the
value of its resulting move-phase, then minimaxed. Without this, a ply-1
action that triggers a forced switch would "waste" ply-2 on the switch itself,
hiding setup payoff (e.g. Tailwind T1 into Darmanitan switch-in + 2 attacks T2).

Subway AI pruning: p2's voluntary switches are stripped during enumeration
(see BattleSubwayAI.md — the AI never voluntarily switches). Forced post-KO
switches are unaffected.
"""

import math


# p2 is the Subway trainer; strip voluntary switches from their action space.
DEFAULT_SUBWAY_AI_SIDES = ["p2"]


def _decode_score(v):
    # JS encodes Infinity as strings since JSON has no native Infinity.
    if v == "inf":
        return math.inf
    if v == "-inf":
        return -math.inf
    return v


def evaluate_state(state):
    """Score a (non-terminal) battle state from p1's perspective.

    Kept for callers that want a raw HP-fraction eval without running a turn.
    """
    def _hp_fraction(mon):
        if mon.get("fainted"):
            return 0.0
        maxhp = mon.get("maxhp") or 0
        return (mon["hp"] / maxhp) if maxhp else 0.0
    sides = state["sides"]
    p1 = sum(_hp_fraction(m) for m in sides[0]["pokemon"])
    p2 = sum(_hp_fraction(m) for m in sides[1]["pokemon"])
    return p1 - p2


def clear_cache(drv):
    return drv.call("clear_minimax_cache")


def cache_stats(drv=None):
    # Back-compat: demo_rollout calls cache_stats() with no args after every
    # decision. The JS RPC returns the same info in its minimax response, so
    # stale-reads here are acceptable; just return 0 if no drv is provided.
    if drv is None:
        return {"entries": 0}
    return drv.call("minimax_cache_stats")


def best_pair_minimax(drv, state, enum=None, subway_ai_sides=None, p2_policy="minimax", assume_hit=False):
    """Depth-1 adversarial minimax. Returns (chosen_pair, scored_list, profile).

    scored_list is [(p1_action, score, adversarial_p2_action), ...] sorted
    by score desc — matches the pre-shim return shape.
    `profile` is a dict of driver-side timing buckets (see handleBestPairMinimax).
    `p2_policy` ∈ {"minimax", "greedy"}; see driver.js for semantics. `greedy`
    collapses p2 branching to the scripted Subway-AI model.
    `assume_hit`: if True, all accuracy rolls resolve as hits inside search —
    both in the static damage matrix and on cloned battles used for deeper plies.
    `enum` is accepted for API compatibility but ignored (JS re-enumerates).
    """
    sides = DEFAULT_SUBWAY_AI_SIDES if subway_ai_sides is None else subway_ai_sides
    res = drv.call("best_pair_minimax", {
        "state": state, "depth": 1, "subway_ai_sides": sides, "p2_policy": p2_policy,
        "assume_hit": assume_hit,
    })
    pair = res["chosen_pair"]
    scored = [(r["p1"], _decode_score(r["d1"]), r["adv_p2"]) for r in res["scored"]]
    return pair, scored, res.get("profile", {})


def best_pair_minimax_depth2(drv, state, enum=None, top_k=5, subway_ai_sides=None, p2_policy="minimax", assume_hit=False, extended_scores=False):
    """Depth-2 adversarial minimax with top-K filtering on p1 actions.

    Returns (chosen_pair, scored, profile) where scored is a list of
    (p1_action, d1_score, d2_score, adversarial_p2_action) sorted by d2 desc.

    When `extended_scores=True`, each scored entry gains two extra fields —
    `d2_worst` (worst-case-collapse score, p1 misses + min damage, p2 hits +
    max damage; the Cat-1-proof signal) and `d2_expected` (probability-weighted
    EV across accuracy hit/miss outcomes). Selection switches to: prefer
    candidates with `d2_worst > 0`, breaking ties by `d2_expected`; fall back
    to max `d2_expected` when no Cat-1-able line exists. Each scored entry is
    `(p1, d1, d2, adv_p2)` or `(p1, d1, d2, adv_p2, d2_worst, d2_expected)`.

    `profile` is a dict of driver-side timing buckets.
    `p2_policy` ∈ {"minimax", "greedy"} (see driver.js).
    `assume_hit`: if True, all accuracy rolls resolve as hits inside search.
    `enum` is accepted for API compatibility but ignored (JS re-enumerates).
    """
    sides = DEFAULT_SUBWAY_AI_SIDES if subway_ai_sides is None else subway_ai_sides
    res = drv.call("best_pair_minimax", {
        "state": state, "depth": 2, "top_k": top_k, "subway_ai_sides": sides,
        "p2_policy": p2_policy, "assume_hit": assume_hit,
        "extended_scores": extended_scores,
    })
    pair = res["chosen_pair"]
    scored = []
    for r in res["scored"]:
        row = (r["p1"], _decode_score(r["d1"]), _decode_score(r["d2"]), r["adv_p2"])
        if extended_scores:
            row = row + (_decode_score(r["d2_worst"]), _decode_score(r["d2_expected"]))
        scored.append(row)
    return pair, scored, res.get("profile", {})


def format_profile(p):
    """One-line summary of a driver-side minimax profile dict."""
    if not p:
        return ""
    def ms(k):
        v = p.get(k, 0)
        return f"{v:.0f}ms"
    br = f"{p.get('p1_actions_root', 0)}x{p.get('p2_actions_root', 0)}"
    buckets = (
        f"clone={ms('clone_ms')} apply={ms('apply_ms')} "
        f"ser={ms('serialize_ms')} enum={ms('enumerate_ms')} "
        f"eval={ms('eval_ms')} static={ms('static_eval_ms')}"
    )
    cache = f"cache={p.get('cache_hits', 0)}h/{p.get('cache_misses', 0)}m"
    return (
        f"branch={br} pairs={p.get('pairs_scored', 0)} "
        f"recurse={p.get('recurse_calls', 0)} {buckets} {cache}"
    )
