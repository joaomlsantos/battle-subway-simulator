"""Shared rollout loop: given a driver and two teams, run a battle to
completion using adversarial depth-2 minimax for p1 and print per-turn state.

Extracted from demo_rollout.py so both the fixed-opponent demo and the
trainer-driven demo ([src/demo_trainer_rollout.py]) can share the same
loop. No data-structure changes vs the prior inline version.
"""
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from typing import List, Dict, Optional

from evaluator import best_pair_minimax_depth2, cache_stats, format_profile


DEFAULT_DEPTH2_TOP_K = 20
DEFAULT_P2_POLICY = "minimax"  # "minimax" = adversarial; "greedy" = scripted Subway-AI
DEFAULT_ASSUME_HIT = True
DEFAULT_EXTENDED_SCORES = True
DEFAULT_MAX_TURNS = 60

_LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs"))


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)

    def flush(self):
        for st in self.streams:
            st.flush()


@contextmanager
def _tee_stdout(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "w", encoding="utf-8")
    orig = sys.stdout
    sys.stdout = _Tee(orig, f)
    try:
        yield path
    finally:
        sys.stdout = orig
        f.close()


def _default_log_path(label: Optional[str] = None) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{label}" if label else ""
    return os.path.join(_LOG_DIR, f"rollout-{stamp}{suffix}.log")


def format_action(a):
    if a is None:
        return "-"
    if a["type"] == "pass":
        return "pass"
    if a["type"] == "switch":
        return f"switch->{a['to']}"
    tgt = f" @{a['target']}" if "target" in a else ""
    return f"{a['move']}{tgt}"


def format_side(actions):
    if actions is None:
        return "(no request)"
    return " | ".join(format_action(a) for a in actions)


def format_boosts(boosts):
    if not boosts: return ""
    parts = [f"{k}{v:+d}" for k, v in boosts.items() if v]
    return f" [{' '.join(parts)}]" if parts else ""


def format_mon(mon, show_hp_if_full=False):
    name = mon["set"]["name"]
    if mon.get("fainted"):
        return f"{name} FNT"
    hp, maxhp = mon["hp"], mon["maxhp"]
    pct = int(round(100 * hp / maxhp)) if maxhp else 0
    parts = [name]
    if pct < 100 or show_hp_if_full:
        parts.append(f"{pct}%")
    if mon.get("status"):
        parts.append(mon["status"])
    return " ".join(parts) + format_boosts(mon.get("boosts", {}))


def format_side_state(side):
    actives = [m for m in side["pokemon"] if m.get("isActive")]
    bench = [m for m in side["pokemon"] if not m.get("isActive")]
    active_str = "  ".join(format_mon(m, show_hp_if_full=True) for m in actives)
    bench_str = ", ".join(format_mon(m) for m in bench) if bench else "-"
    return f"active: {active_str}  | bench: {bench_str}"


def rng_summary(events):
    if not events:
        return ""
    counts = {}
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    unknowns = [e["caller"] for e in events if e["kind"] == "unknown"]
    parts = [f"{k}:{v}" for k, v in sorted(counts.items())]
    summary = " ".join(parts)
    if unknowns:
        summary += f"  [unknown callers: {sorted(set(unknowns))}]"
    return summary


def _short_actor(s):
    # "p1.slot0" -> "p1s0"
    if not s:
        return "?"
    return s.replace(".slot", "s")


def _format_event_outcome(e):
    """Format a single rng_event outcome compactly. Returns None for kinds we
    don't surface (shuffle, etc.)."""
    kind = e["kind"]
    out = e["outcome"]
    forced = " (forced)" if e.get("forced") else ""
    if kind == "accuracy":
        return ("hit" if out else "miss") + forced
    if kind == "crit":
        return ("crit" if out else "no_crit") + forced
    if kind == "damage_roll":
        # outcome is the roll int 0..15; 0 = max dmg, 15 = min dmg
        return f"roll={out}" + forced
    if kind == "secondary":
        # outcome is 0..99 (random(100)); proc iff outcome < chance, but we
        # can't know `chance` here without the move. Just show the roll.
        return f"sec_roll={out}" + forced
    if kind == "fullpara":
        return ("FULL_PARA" if out else "moved") + forced
    if kind == "confusion_self_hit":
        return ("CONFUSED_SELF_HIT" if out else "moved") + forced
    if kind == "sleep_turn":
        return ("ASLEEP" if out else "woke") + forced
    if kind == "freeze_turn":
        return ("FROZEN" if out else "thawed") + forced
    if kind == "attract_immobilize":
        return ("INFATUATED" if out else "moved") + forced
    if kind == "redirect_target":
        return None  # noisy, not user-visible
    if kind == "speed_tie":
        return None
    if kind == "stall":
        return None
    if kind == "contact_ability":
        return f"contact_ability:{out}"
    return None


def rng_per_move(events):
    """Format rng events grouped by (actor, move). Surfaces outcomes the
    player cares about — crits, hit/miss, paralysis turn rolls, secondary
    procs — and elides plumbing rolls (redirect_target, speed_tie, stall)."""
    if not events:
        return ""
    groups = []  # list of {actor, move, parts}
    for e in events:
        actor = _short_actor(e.get("actor"))
        move = e.get("move") or ""
        outcome = _format_event_outcome(e)
        if outcome is None:
            continue
        # New group when (actor, move) changes from the previous emitted group.
        if not groups or groups[-1]["actor"] != actor or groups[-1]["move"] != move:
            groups.append({"actor": actor, "move": move, "parts": []})
        groups[-1]["parts"].append(outcome)
    out = []
    for g in groups:
        if not g["parts"]:
            continue
        label = f"{g['actor']}:{g['move']}" if g["move"] else g["actor"]
        out.append(f"{label}[{','.join(g['parts'])}]")
    return " ".join(out)


def _run_rollout_inner(
    drv,
    p1_team: List[Dict],
    p2_team: List[Dict],
    *,
    header_lines: Optional[List[str]],
    top_k: int,
    p2_policy: str,
    assume_hit: bool,
    extended_scores: bool,
    max_turns: int,
    seed: Optional[List[int]],
) -> Dict:
    for line in header_lines or []:
        print(line)
    if header_lines:
        print()
    if seed is not None:
        print(f"seed: {seed}")
        print()

    start = drv.start_battle(p1_team, p2_team, seed=seed)
    state = start["state"]

    print(f"P1: {[m.get('species') or m.get('name') for m in p1_team]}")
    print(f"P2: {[m.get('species') or m.get('name') for m in p2_team]}")
    print()

    summary_lines: List[str] = []
    step = 0
    turn_counter = 0
    winner = None
    ended = False
    while step < max_turns:
        step += 1
        enum = drv.enumerate_turn(state)
        req = enum.get("requestState")
        pairs = enum.get("action_pairs", [])
        if not pairs:
            msg = f"[step {step}] no enumerable actions (requestState={req}); stopping"
            print(msg)
            summary_lines.append(msg)
            break

        t0 = time.perf_counter()
        pair, scored, profile = best_pair_minimax_depth2(
            drv, state, enum=enum, top_k=top_k, p2_policy=p2_policy,
            assume_hit=assume_hit, extended_scores=extended_scores,
        )
        elapsed = time.perf_counter() - t0
        if pair is None:
            msg = f"[step {step}] minimax returned no pair; stopping"
            print(msg)
            summary_lines.append(msg)
            break

        tag = "switch" if req == "switch" else "picking"
        stats = cache_stats(drv)
        print(f"[{tag:>8}] top-{top_k} depth-2 in {elapsed:.1f}s (cache: {stats['entries']} entries)")
        print(f"         profile: {format_profile(profile)}")
        for entry in scored:
            p1_action, d1, d2, adv_p2 = entry[0], entry[1], entry[2], entry[3]
            marker = "->" if p1_action == pair["p1"] else "  "
            if extended_scores and len(entry) >= 6:
                d2_worst, d2_expected = entry[4], entry[5]
                print(f"         {marker} {format_side(p1_action):<48}  "
                      f"d1={d1:+.2f}  d2={d2:+.2f}  worst={d2_worst:+.2f}  exp={d2_expected:+.2f}  "
                      f"vs {format_side(adv_p2)}")
            else:
                print(f"         {marker} {format_side(p1_action):<48}  d1={d1:+.2f}  d2={d2:+.2f}  "
                      f"vs {format_side(adv_p2)}")

        # Real RNG on the actual battle — assume_hit only steers the search.
        turn_result = drv.execute_turn(
            state, {"p1": pair["p1"], "p2": pair["p2"]},
        )
        state = turn_result["state"]
        turn_counter = turn_result["turn"]

        tag = "switch" if req == "switch" else f"turn {turn_counter}"
        head = (f"[{tag:>8}] P1 {format_side(pair['p1']):<48} || P2 {format_side(pair['p2']):<48}"
                f"  rng[{rng_summary(turn_result['rng_events'])}]")
        per_move = rng_per_move(turn_result["rng_events"])
        rng_line = f"           rng: {per_move}" if per_move else None
        sides = turn_result["state"]["sides"]
        p1_line = f"           P1 {format_side_state(sides[0])}"
        p2_line = f"           P2 {format_side_state(sides[1])}"
        print(head)
        if rng_line:
            print(rng_line)
        print(p1_line)
        print(p2_line)
        summary_lines.append(head)
        if rng_line:
            summary_lines.append(rng_line)
        summary_lines.append(p1_line)
        summary_lines.append(p2_line)

        if turn_result["ended"]:
            winner = turn_result["winner"]
            ended = True
            end_msg = f"battle ended on step {step}. winner: {winner}. turn counter: {turn_counter}"
            print(f"\n{end_msg}")
            summary_lines.append("")
            summary_lines.append(end_msg)
            break
    else:
        timeout_msg = f"reached max_turns={max_turns} without termination"
        print(f"\n{timeout_msg}")
        summary_lines.append("")
        summary_lines.append(timeout_msg)

    print()
    print("===== SUMMARY =====")
    for line in header_lines or []:
        print(line)
    if header_lines:
        print()
    print(f"P1: {[m.get('species') or m.get('name') for m in p1_team]}")
    print(f"P2: {[m.get('species') or m.get('name') for m in p2_team]}")
    print()
    for line in summary_lines:
        print(line)

    return {"winner": winner, "turn": turn_counter, "steps": step, "ended": ended}


def run_rollout(
    drv,
    p1_team: List[Dict],
    p2_team: List[Dict],
    *,
    header_lines: Optional[List[str]] = None,
    log_path: Optional[str] = None,
    top_k: int = DEFAULT_DEPTH2_TOP_K,
    p2_policy: str = DEFAULT_P2_POLICY,
    assume_hit: bool = DEFAULT_ASSUME_HIT,
    extended_scores: bool = DEFAULT_EXTENDED_SCORES,
    max_turns: int = DEFAULT_MAX_TURNS,
    quiet: bool = False,
    seed: Optional[List[int]] = None,
    log_label: Optional[str] = None,
) -> Dict:
    """Drive one battle to completion. Prints per-turn state as a side effect
    and tees output to a timestamped logfile under ./logs/. With `quiet=True`,
    output goes only to the logfile (no console) and the trailing "log written
    to" line is suppressed — for batch enumerators that track progress externally.
    `seed` is the 4-int PS PRNG seed; pass None to use the driver default
    ([1,2,3,4]); enumerators should pass a fresh random seed per battle to
    avoid every matchup replaying the same RNG sequence. `log_label` is an
    optional context tag appended to the timestamped filename so a sweep's
    logs are findable without grepping their headers.
    Returns {winner, turn, steps, ended}.
    """
    path = log_path or _default_log_path(log_label)
    if quiet:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        orig = sys.stdout
        with open(path, "w", encoding="utf-8") as f:
            sys.stdout = f
            try:
                return _run_rollout_inner(
                    drv, p1_team, p2_team,
                    header_lines=header_lines,
                    top_k=top_k, p2_policy=p2_policy,
                    assume_hit=assume_hit, extended_scores=extended_scores,
                    max_turns=max_turns, seed=seed,
                )
            finally:
                sys.stdout = orig
    with _tee_stdout(path) as p:
        result = _run_rollout_inner(
            drv, p1_team, p2_team,
            header_lines=header_lines,
            top_k=top_k, p2_policy=p2_policy,
            assume_hit=assume_hit, extended_scores=extended_scores,
            max_turns=max_turns, seed=seed,
        )
    print(f"\nlog written to {p}")
    return result
