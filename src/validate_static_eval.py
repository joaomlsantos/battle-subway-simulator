"""Cross-check the static evaluator against real PS execution.

For a given (state, action pair), the driver's staticScorePair() predicts the
post-turn HP-sum delta (p1 − p2) without running the sim. This script measures
prediction error by running both the static eval (via best_pair_minimax
depth=1, which returns per-p1 worst-over-p2 d1 scores) and the real turn in
PS, then reporting residuals for each chosen (p1, adv_p2) pair.

Signal to watch:
- mean |residual| : average prediction error in HP-fraction units (0..4 scale)
- max residual    : worst miss
- rank agreement  : does the static d1 ordering match the actual-outcome ordering?
                    (we only compare top-10 to keep it tractable)

Usage: python src/validate_static_eval.py
"""
import math
import time
from ps_driver import PSDriver


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


def hp_sum(side):
    total = 0.0
    for m in side["pokemon"]:
        if m.get("fainted"):
            continue
        maxhp = m.get("maxhp") or 0
        total += (m["hp"] / maxhp) if maxhp else 0
    return total


def evaluate(state):
    sides = state["sides"]
    return hp_sum(sides[0]) - hp_sum(sides[1])


def decode(v):
    if v == "inf":
        return math.inf
    if v == "-inf":
        return -math.inf
    return v


def validate_single_state(drv, state, label, limit=20):
    pre_eval = evaluate(state)
    t0 = time.perf_counter()
    res = drv.call("best_pair_minimax", {
        "state": state, "depth": 1, "subway_ai_sides": ["p2"], "p2_policy": "minimax",
    })
    d1_wall = time.perf_counter() - t0
    scored = res["scored"]
    prof = res.get("profile", {})
    print(f"\n=== {label} ===")
    print(f"pre-turn eval: {pre_eval:+.3f}   d1 wall: {d1_wall*1000:.0f}ms   "
          f"pairs={prof.get('pairs_scored', 0)}  "
          f"p1_actions={prof.get('p1_actions_root', 0)}  p2_actions={prof.get('p2_actions_root', 0)}  "
          f"clone={prof.get('clone_ms', 0):.0f}ms apply={prof.get('apply_ms', 0):.0f}ms "
          f"ser={prof.get('serialize_ms', 0):.0f}ms  total={prof.get('total_ms', 0):.0f}ms")

    rows = []
    for r in scored[:limit]:
        predicted_d1 = decode(r["d1"])
        if not math.isfinite(predicted_d1):
            rows.append((r["p1"], r["adv_p2"], predicted_d1, None, None))
            continue
        # Apply the predicted-worst pair in real PS.
        exec_res = drv.execute_turn(state, {"p1": r["p1"], "p2": r["adv_p2"]})
        post_eval = evaluate(exec_res["state"])
        actual_delta = post_eval - pre_eval
        # predicted_d1 is absolute eval at the new state (our code adds it to
        # absolute post-turn hp sums; verify).
        # Actually: staticScorePair returns p1_sum − p2_sum absolute, not delta.
        # So compare predicted_d1 vs actual post_eval.
        residual = predicted_d1 - post_eval
        rows.append((r["p1"], r["adv_p2"], predicted_d1, post_eval, residual))

    # Formatting helpers
    def fmt_action(a):
        if a is None:
            return "-"
        if isinstance(a, list):
            return " | ".join(fmt_action(x) for x in a)
        if a.get("type") == "pass":
            return "pass"
        if a.get("type") == "switch":
            return f"sw->{a['to']}"
        tgt = f"@{a['target']}" if "target" in a else ""
        return f"{a['move']}{tgt}"

    print(f"{'p1 action':<60} {'adv_p2':<40} {'d1_pred':>8} {'actual':>8} {'resid':>8}")
    resids = []
    for p1_a, p2_a, pred, actual, res in rows:
        p1_s = fmt_action(p1_a)[:58]
        p2_s = fmt_action(p2_a)[:38]
        if actual is None:
            print(f"{p1_s:<60} {p2_s:<40} {pred:+8.2f} {'inf':>8} {'--':>8}")
        else:
            print(f"{p1_s:<60} {p2_s:<40} {pred:+8.2f} {actual:+8.2f} {res:+8.2f}")
            resids.append(res)

    if resids:
        mean_abs = sum(abs(r) for r in resids) / len(resids)
        max_abs = max(abs(r) for r in resids)
        mean_signed = sum(resids) / len(resids)
        print(f"\nresidual summary (top-{len(resids)}): "
              f"mean|r|={mean_abs:.3f}  max|r|={max_abs:.3f}  mean_signed={mean_signed:+.3f}  "
              f"(>0 = static is optimistic, <0 = pessimistic)")

    return rows


def main():
    with PSDriver() as drv:
        p1_team = [TERRAKION, WHIMSICOTT, DARMANITAN, THUNDURUS]
        p2_team = [GARCHOMP, LATIOS, METAGROSS, HYDREIGON]

        start = drv.start_battle(p1_team, p2_team)
        validate_single_state(drv, start["state"], "Turn 1 opener", limit=15)

        # Play out the "intended" Beat Up @ally opener and validate again mid-game.
        turn1 = drv.execute_turn(start["state"], {
            "p1": [
                {"type": "move", "move": "X-Scissor", "target": "foeSlot0"},
                {"type": "move", "move": "Beat Up", "target": "allySlot0"},
            ],
            "p2": [
                {"type": "move", "move": "Earthquake"},
                {"type": "move", "move": "Draco Meteor", "target": "foeSlot1"},
            ],
        })
        state = turn1["state"]
        # Forced switch phase if Whimsicott fainted — send in Darmanitan.
        enum = drv.enumerate_turn(state)
        if enum.get("requestState") == "switch":
            res = drv.execute_turn(state, {
                "p1": [{"type": "pass"}, {"type": "switch", "to": "Darmanitan"}],
                "p2": None,
            })
            state = res["state"]
        validate_single_state(drv, state, "Post-opener mid-game", limit=15)


if __name__ == "__main__":
    raise SystemExit(main())
