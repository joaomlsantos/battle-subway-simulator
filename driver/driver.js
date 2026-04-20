// Thin Node.js driver that wraps Pokemon Showdown's Gen 5 sim.
// Protocol: line-delimited JSON-RPC on stdin/stdout.
//   Request:  {"id": "<string>", "method": "<name>", "params": {...}}
//   Response: {"id": "<string>", "result": {...}}  or  {"id": "<string>", "error": "<msg>"}
//
// Methods:
//   start_battle    — {teams: {p1:[sets], p2:[sets]}, seed?}        → {state}
//   execute_turn    — {state, actions, rng_overrides?}              → {state, rng_events}
//   damage_range    — {attacker, defender, move}                    → {min, max, ...}
//   enumerate_turn  — (stub)
//
// Build PS first: `cd pokemon-showdown && node build`.


const readline = require('readline');
const path = require('path');
const crypto = require('crypto');

const PS_DIST = path.resolve(__dirname, '..', 'pokemon-showdown', 'dist', 'sim');
const { Battle, Dex } = require(PS_DIST);
const { buildStaticEvalContext, staticScorePair } = require('./static_eval.js');
const { State } = require(path.join(PS_DIST, 'state'));


const FORMAT_ID = 'gen5doublescustomgame';
const DEFAULT_SEED = [1, 2, 3, 4];


function normalizeSet(set) {
    const species = set.species || set.name;
    return {
        name: set.name || species,
        species,
        item: set.item || '',
        ability: set.ability || '',
        moves: (set.moves || []).slice(0, 4),
        nature: set.nature || 'Serious',
        gender: set.gender || '',
        evs: { hp: 0, atk: 0, def: 0, spa: 0, spd: 0, spe: 0, ...(set.evs || {}) },
        ivs: { hp: 31, atk: 31, def: 31, spa: 31, spd: 31, spe: 31, ...(set.ivs || {}) },
        level: set.level || 50,
        happiness: set.happiness !== undefined ? set.happiness : 255,
    };
}


function buildBattle(p1Team, p2Team, seed = DEFAULT_SEED) {
    const format = Dex.formats.get(FORMAT_ID, true);
    const battle = new Battle({
        formatid: format.id,
        format,
        p1: { name: 'P1', team: p1Team.map(normalizeSet) },
        p2: { name: 'P2', team: p2Team.map(normalizeSet) },
        seed,
        strictChoices: true,
        send: () => {},
    });
    if (battle.requestState === 'teampreview') {
        battle.makeChoices('default', 'default');
    }
    return battle;
}


function restoreBattle(serialized) {
    const battle = State.deserializeBattle(serialized);
    battle.send = () => {};
    return battle;
}


// Roll code: 0 = 100% damage, 15 = 85% damage. See battle.ts:2404.
function rollToMultiplier(roll) {
    return (baseDamage) => Math.floor(baseDamage * (100 - roll) / 100);
}

function parseRollSpec(force) {
    if (typeof force === 'number') return force;
    if (force === 'max') return 0;
    if (force === 'min') return 15;
    throw new Error(`unknown damage_roll force: ${force}`);
}

function forceDamageRoll(battle, roll) {
    battle.randomizer = rollToMultiplier(roll);
}


function actorId(pokemon) {
    if (!pokemon || !pokemon.side) return null;
    const sideIdx = pokemon.side.n; // 0 for p1, 1 for p2
    const slotIdx = pokemon.side.active.indexOf(pokemon);
    return `p${sideIdx + 1}.slot${slotIdx}`;
}


// Translate one slot action (our JSON shape) into the PS choice-string fragment.
// Choice fragments: "move 1", "move 2 +1", "switch 3", "pass".
// PS target encoding: +N targets opponent slot N-1; -N targets ally slot N-1.
function formatSlotChoice(action, activeMon, side) {
    if (!action || action.type === 'pass') return 'pass';
    if (action.type === 'switch') {
        if (!action.to) throw new Error('switch action requires "to"');
        const idx = side.pokemon.findIndex(p => p.species && p.species.name === action.to);
        if (idx < 0) throw new Error(`no bench pokemon named ${action.to} on side ${side.id}`);
        return `switch ${idx + 1}`;
    }
    if (action.type === 'move') {
        if (!activeMon) throw new Error('move action requires a live active pokemon');
        const moveId = Dex.toID(action.move);
        const moveIdx = activeMon.moves.indexOf(moveId);
        if (moveIdx < 0) throw new Error(`move ${action.move} (${moveId}) not on ${activeMon.species.name}'s moveset [${activeMon.moves.join(', ')}]`);
        const tgt = targetToChoiceFragment(action.target);
        return tgt ? `move ${moveIdx + 1} ${tgt}` : `move ${moveIdx + 1}`;
    }
    throw new Error(`unknown action type: ${action.type}`);
}

function targetToChoiceFragment(target) {
    if (!target) return '';
    switch (target) {
        case 'foeSlot0': return '+1';
        case 'foeSlot1': return '+2';
        case 'allySlot0': return '-1';
        case 'allySlot1': return '-2';
        default: return ''; // spread / self / ally-side conditions have no target arg
    }
}

function formatSideChoice(slotActions, side) {
    // null/undefined side action = "this side has no pending request" (switch phase,
    // only the other side fainted). makeChoices skips sides with empty input.
    if (slotActions == null) return '';
    const parts = [];
    for (let i = 0; i < 2; i++) {
        const action = slotActions[i] || { type: 'pass' };
        parts.push(formatSlotChoice(action, side.active[i], side));
    }
    return parts.join(', ');
}


// Map stack frame to a semantic RNG kind. Keyed by "Class.method" extracted
// from the first PS frame above our wrapper. Pinned to current PS dist.
const KIND_BY_CALLER = {
    'Battle.randomizer': 'damage_roll',
    'BattleActions.hitStepAccuracy': 'accuracy',
    'BattleActions.tryMoveHit': 'multi_hit',
    'BattleActions.hitStepMoveHitLoop': 'multi_hit',  // sample(weighted[20]) for 2-5 hit count
    'BattleActions.hitStepTryHitEvent': 'accuracy',
    'BattleActions.moveHit': 'secondary',
    'BattleActions.secondaries': 'secondary',
    'BattleActions.selfDrops': 'secondary',       // random(100) < self.chance (same shape as secondaries)
    'BattleActions.getDamage': 'crit',
    'BattleActions.getMoveTargets': 'redirect_target',
    'Battle.speedSort': 'speed_tie',
    'Battle.getTarget': 'redirect_target',
    'Battle.getRandomSwitchable': 'random_switchable',
    'Side.randomFoe': 'redirect_target',          // sample(foes) — default target when intended is gone
};

// Frames to skip when walking the stack looking for the real PS caller.
// Our wrappers appear as lowercase instance names (prng.random) because they
// are assigned to a local `prng` variable. PS's internal methods show class
// names (PRNG.randomChance, Battle.randomChance).
const SKIP_FRAMES = new Set([
    'prng.random', 'prng.randomChance', 'prng.sample', 'prng.shuffle',  // our wrappers
    'PRNG.random', 'PRNG.randomChance', 'PRNG.sample', 'PRNG.shuffle',
    'Battle.random', 'Battle.randomChance', 'Battle.sample',
]);

// Frames that indicate we're inside PRNG plumbing (used for dedup: when
// prng.random is reached via PRNG.randomChance, the outer randomChance
// wrapper will emit — we suppress the inner random emit).
const PRNG_INTERNAL_FRAMES = new Set([
    'PRNG.random', 'PRNG.randomChance', 'PRNG.sample', 'PRNG.shuffle',
]);

function stackFrames(stack) {
    const out = [];
    for (const line of stack.split('\n')) {
        const m = line.match(/at\s+([A-Za-z0-9_$.<>]+)\s*\(/);
        if (m) out.push(m[1]);
    }
    return out;
}

function parseCaller(stack) {
    for (const fn of stackFrames(stack)) {
        if (SKIP_FRAMES.has(fn)) continue;
        return fn;
    }
    return null;
}

function isInnerPrngCall(stack) {
    // Walk frames; first non-our-wrapper frame is the direct caller of prng.random.
    // If that frame is a PRNG internal, we're inside randomChance/sample/shuffle.
    for (const fn of stackFrames(stack)) {
        if (fn === 'prng.random' || fn === 'prng.randomChance' || fn === 'prng.sample') continue;
        return PRNG_INTERNAL_FRAMES.has(fn);
    }
    return false;
}

// Event-dispatch callers (Battle.onStart / onBeforeMove / onDamagingHit / etc.)
// are catch-alls whose semantics depend on the *effect* whose handler is
// running. battle.effect tells us which. For unambiguous callers (onStallMove,
// onDamagingHit) a single kind covers all uses.
function inferKind(caller, battle) {
    const direct = KIND_BY_CALLER[caller];
    if (direct) return direct;
    const effectId = (battle.effect && battle.effect.id) || '';
    if (caller === 'Battle.onStallMove') return 'stall';
    if (caller === 'Battle.onDamagingHit') return 'contact_ability';
    if (caller === 'Battle.onBeforeMove') {
        if (effectId === 'confusion') return 'confusion_self_hit';
        if (effectId === 'par') return 'fullpara';
        if (effectId === 'slp') return 'sleep_turn';
        if (effectId === 'frz') return 'freeze_turn';
        if (effectId === 'attract') return 'attract_immobilize';
    }
    if (caller === 'Battle.onStart') {
        if (effectId === 'confusion') return 'confusion_duration';
        if (effectId === 'slp') return 'sleep_duration';
        if (effectId === 'taunt') return 'taunt_duration';
        if (effectId === 'encore') return 'encore_duration';
        if (effectId === 'disable') return 'disable_duration';
    }
    return 'unknown';
}


// Per-kind mapping of a semantic `force` value to the raw return value that
// short-circuits the underlying primitive. E.g. damage_roll min damage is
// random(16) returning 15; accuracy hit is randomChance returning true.
function forceValue(kind, force) {
    switch (kind) {
        case 'damage_roll':
            if (force === 'max') return 0;
            if (force === 'min') return 15;
            if (typeof force === 'number' && force >= 0 && force <= 15) return force;
            break;
        case 'accuracy':
            if (force === 'hit') return true;
            if (force === 'miss') return false;
            break;
        case 'crit':
            if (force === 'crit') return true;
            if (force === 'no_crit') return false;
            break;
        case 'secondary':
            // secondary triggers when random(100) < chance; 0 always procs, 99 never procs
            // (for chances < 100 — a 100% secondary has no RNG call at all in practice).
            if (force === 'proc') return 0;
            if (force === 'no_proc') return 99;
            if (typeof force === 'number' && force >= 0 && force <= 99) return force;
            break;
    }
    throw new Error(`unsupported force "${force}" for kind "${kind}"`);
}


function findOverride(overrides, kind, battle) {
    if (!overrides.length) return null;
    const src = battle.activePokemon;
    const move = battle.activeMove;
    const actorStr = src ? actorId(src) : null;
    const moveStr = move && move.name ? move.name : null;
    return overrides.find(o =>
        o.kind === kind &&
        (!o.actor || o.actor === actorStr) &&
        (!o.move || o.move === moveStr)
    ) || null;
}

// Observational-only PRNG interception. Wraps battle.prng.random — every
// randomChance/sample/shuffle funnels through it, so one wrap catches them all.
// Forcing is not implemented yet; overrides are logged as no-ops so we can see
// what event stream a real turn produces before designing the override layer.
function installRngHooks(battle, overrides, events) {
    const prng = battle.prng;
    const origRandom = prng.random.bind(prng);
    const origRandomChance = prng.randomChance.bind(prng);
    const origSample = prng.sample.bind(prng);
    const origShuffle = prng.shuffle.bind(prng);

    // Dispatch for random/randomChance/sample: resolve kind, check overrides,
    // either force or roll, and emit a single semantic event.
    function dispatch(primitive, stk, args, rollOriginal) {
        const caller = parseCaller(stk);
        const kind = inferKind(caller, battle);
        const effectId = (battle.effect && battle.effect.id) || null;
        const match = findOverride(overrides, kind, battle);

        let outcome;
        let forced = false;
        if (match) {
            outcome = forceValue(kind, match.force);
            forced = true;
        } else {
            outcome = rollOriginal();
        }

        events.push({ kind, caller, effect: effectId, primitive, args, outcome, forced });
        return outcome;
    }

    prng.random = function(from, to) {
        const stk = new Error().stack;
        // If called via randomChance/sample/shuffle, let the outer wrapper handle it.
        if (isInnerPrngCall(stk)) return origRandom(from, to);
        const args = [from, to].filter(x => x !== undefined);
        return dispatch('random', stk, args, () => origRandom(from, to));
    };

    prng.randomChance = function(numerator, denominator) {
        return dispatch('randomChance', new Error().stack,
            [numerator, denominator],
            () => origRandomChance(numerator, denominator));
    };

    prng.sample = function(items) {
        return dispatch('sample', new Error().stack,
            [`array[${items.length}]`],
            () => origSample(items));
    };

    prng.shuffle = function(items, start, end) {
        // shuffle doesn't yet support forcing; emit observational event only.
        const caller = parseCaller(new Error().stack);
        const labelItem = (it) => {
            if (it && it.effect && it.effect.name) return it.effect.name;
            if (it && it.pokemon && it.pokemon.species) return it.pokemon.species.name;
            return typeof it === 'object' ? '<obj>' : String(it);
        };
        const before = items.slice(start, end).map(labelItem);
        origShuffle(items, start, end);
        const after = items.slice(start, end).map(labelItem);
        events.push({
            kind: inferKind(caller, battle),
            caller,
            effect: (battle.effect && battle.effect.id) || null,
            primitive: 'shuffle',
            args: [`array[${items.length}]`, start, end],
            outcome: { before, after },
            forced: false,
        });
    };
}


// --- handlers ---

function handleStartBattle(params) {
    const { teams, seed } = params;
    if (!teams || !teams.p1 || !teams.p2) {
        throw new Error('start_battle requires teams.p1 and teams.p2');
    }
    const battle = buildBattle(teams.p1, teams.p2, seed || DEFAULT_SEED);
    return { state: State.serializeBattle(battle) };
}


function handleExecuteTurn(params) {
    const { state, actions, rng_overrides = [] } = params;
    if (!state) throw new Error('execute_turn requires state');
    if (!actions || !('p1' in actions) || !('p2' in actions)) {
        throw new Error('execute_turn requires actions.p1 and actions.p2 (use null for a side with no pending request)');
    }

    const battle = restoreBattle(state);
    const events = [];
    installRngHooks(battle, rng_overrides, events);

    const p1Choice = formatSideChoice(actions.p1, battle.sides[0]);
    const p2Choice = formatSideChoice(actions.p2, battle.sides[1]);
    battle.makeChoices(p1Choice, p2Choice);

    return {
        state: State.serializeBattle(battle),
        rng_events: events,
        choices: { p1: p1Choice, p2: p2Choice }, // echo for debugging
        ended: !!battle.ended,
        winner: battle.winner || null,
        turn: battle.turn,
        requestState: battle.requestState,
    };
}


function handleDamageRange(params) {
    const { attacker, defender, move } = params;
    if (!attacker || !defender || !move) {
        throw new Error('damage_range requires attacker, defender, move');
    }

    const filler = { species: 'Ditto', ability: 'Limber', moves: ['Transform'], nature: 'Serious', level: 1 };
    const battle = buildBattle([attacker, filler], [defender, filler]);

    const attackerMon = battle.sides[0].active[0];
    const defenderMon = battle.sides[1].active[0];
    if (!attackerMon || !defenderMon) {
        throw new Error('battle did not populate active slots');
    }

    forceDamageRoll(battle, 0);
    const maxDmg = battle.actions.getDamage(attackerMon, defenderMon, move, true);
    forceDamageRoll(battle, 15);
    const minDmg = battle.actions.getDamage(attackerMon, defenderMon, move, true);

    const moveObj = battle.dex.moves.get(move);
    const accuracy = moveObj.accuracy === true ? 1.0 : moveObj.accuracy / 100;
    const defenderHp = defenderMon.hp;
    const ohkoGuaranteed = typeof minDmg === 'number' && minDmg >= defenderHp && accuracy === 1.0;
    const ohkoPossible = typeof maxDmg === 'number' && maxDmg >= defenderHp;

    return {
        min: minDmg,
        max: maxDmg,
        defenderHp,
        accuracy,
        ohko: { guaranteed: ohkoGuaranteed, possible: ohkoPossible },
        note: 'skeleton: no spread reduction, no multi-hit handling, no crit option',
    };
}


// Map PS's move.target slug to the list of concrete target slugs our action
// taxonomy uses. Returns [null] when no target arg is needed (spread/self/side
// moves, Struggle's randomNormal, scripted targets, etc.).
function enumerateTargetsForMove(targetSlug, side, slotIdx) {
    
    const NO_TARGET = [null];
    const noTargetSlugs = new Set([
        'self', 'all', 'allAdjacent', 'allAdjacentFoes', 'allies', 'allyTeam',
        'allySide', 'foeSide', 'foeAllies', 'scripted', 'randomNormal',
    ]);
    if (!targetSlug || noTargetSlugs.has(targetSlug)) return NO_TARGET;

    const foe = side.foe;
    const opts = [];
    const pushFoe = (i) => {
        if (foe.active[i] && !foe.active[i].fainted) opts.push(`foeSlot${i}`);
    };
    const pushAlly = (i) => {
        if (i === slotIdx) return;   // can't target self via allySlot
        if (side.active[i] && !side.active[i].fainted) opts.push(`allySlot${i}`);
    };

    switch (targetSlug) {
        case 'normal':
        case 'adjacentFoe':
            pushFoe(0); pushFoe(1);
            break;
        case 'any':
            pushFoe(0); pushFoe(1); pushAlly(0); pushAlly(1);
            break;
        case 'adjacentAlly':
            pushAlly(0); pushAlly(1);
            break;
        case 'adjacentAllyOrSelf':
            // 'self' arg not representable in our taxonomy; PS auto-picks self if no target given.
            pushAlly(0); pushAlly(1);
            if (!opts.length) return NO_TARGET;
            break;
        default:
            return NO_TARGET;
    }
    return opts.length ? opts : NO_TARGET;
}

// When subwayAi is true, voluntary switches are stripped — Battle Subway AI
// has no ConsiderSwitching flag and only switches when forced post-KO (see
// BattleSubwayAI.md). Keeps the action space honest for adversarial scoring.
function enumerateSlotActions(battle, side, slotIdx, subwayAi) {
    const pokemon = side.active[slotIdx];
    if (!pokemon || pokemon.fainted) return [{ type: 'pass' }];

    const req = pokemon.getMoveRequestData();
    const candidates = [];

    for (const m of req.moves) {
        if (m.disabled) continue;
        // PS already reports Beat Up with target "any" in gen5; this guard is
        // defensive belt-and-braces for environments where the request data
        // comes back with a stale target slug.
        if (m.id === 'beatup') m.target = 'any';
        const targets = enumerateTargetsForMove(m.target, side, slotIdx);
        for (const t of targets) {
            const action = { type: 'move', move: m.move };
            if (t) action.target = t;
            candidates.push(action);
        }
    }

    if (!req.trapped && !subwayAi) {
        for (const p of side.pokemon) {
            if (!p || p.fainted) continue;
            if (side.active.includes(p)) continue;
            candidates.push({ type: 'switch', to: p.species.name });
        }
    }

    if (!candidates.length) candidates.push({ type: 'pass' });
    return candidates;
}

// Build per-side choices by combining slot0 × slot1 candidates, filtering
// illegal combos: two slots can't switch to the same bench Pokemon.
function enumerateSideChoices(battle, side, subwayAi) {
    const slot0Opts = enumerateSlotActions(battle, side, 0, subwayAi);
    const slot1Opts = enumerateSlotActions(battle, side, 1, subwayAi);
    const choices = [];
    for (const a0 of slot0Opts) {
        for (const a1 of slot1Opts) {
            if (a0.type === 'switch' && a1.type === 'switch' && a0.to === a1.to) continue;
            choices.push([a0, a1]);
        }
    }
    return choices;
}

// Switch-phase (post-KO mid-turn): each side's activeRequest has a `forceSwitch`
// boolean per slot. True slots must pick a bench switch-in; false slots pass.
// Sides with no activeRequest at all contribute `null` — makeChoices skips them.
//
// Bench-undersupply case: if both slots must switch but only one bench mon
// remains, exactly one slot switches and the other passes (PS accepts pass on
// forceSwitch when there's nothing left to switch to). Required switches =
// min(bench_available, force_slot_count).
function enumerateSideSwitchChoices(side) {
    const req = side.activeRequest;
    if (!req || !req.forceSwitch || !req.forceSwitch.some(Boolean)) return [null];

    const benchAvailable = side.pokemon
        .filter((p, i) => p && !p.fainted && i >= side.active.length)
        .map(p => p.species.name);

    if (!benchAvailable.length) return [null];

    const forceSlotCount = req.forceSwitch.filter(Boolean).length;
    const requiredSwitches = Math.min(benchAvailable.length, forceSlotCount);
    const canPass = benchAvailable.length < forceSlotCount;

    // Per-slot candidate list.
    const perSlot = [];
    for (let i = 0; i < side.active.length; i++) {
        if (req.forceSwitch[i]) {
            const opts = benchAvailable.map(name => ({ type: 'switch', to: name }));
            if (canPass) opts.push({ type: 'pass' });
            perSlot.push(opts);
        } else {
            perSlot.push([{ type: 'pass' }]);
        }
    }

    // Cross-product slot0 × slot1, filter duplicate switch-to and under-filled
    // combos (must use every bench mon we can).
    const out = [];
    for (const a0 of perSlot[0]) {
        for (const a1 of perSlot[1]) {
            if (a0.type === 'switch' && a1.type === 'switch' && a0.to === a1.to) continue;
            const switches = (a0.type === 'switch' ? 1 : 0) + (a1.type === 'switch' ? 1 : 0);
            if (switches !== requiredSwitches) continue;
            out.push([a0, a1]);
        }
    }
    return out.length ? out : [null];
}

// --- Scripted greedy p2 policy ---
//
// Battle Subway AI is empirically greedy: move choice is a deterministic
// function of board state, with a strong TRY_TO_FAINT bias. See
// BattleSubwayAI.md. This module approximates that with a cheap
// basePower × type-effectiveness × STAB scorer per (move, target) — no
// full damage calc (which mutates battle.randomizer and PRNG state).
//
// Returned as a single 2-tuple of slot actions matching the existing action
// shape. Used by minimax to collapse p2's branching from ~30 to 1 per decision.
// Voluntary switches are not emitted (AI doesn't volunteer — the four
// documented exceptions in BattleSubwayAI.md are deferred).
//
// Scoring (higher is better):
//   damaging move vs foe target: basePower * 2^effectiveness * (1.5 if STAB else 1)
//   immune target → 0; spread → sum over foes, minus ally-hit penalty
//   status move: fixed baseline (Protect 35, self-boost 40, other 30)
// Accuracy isn't factored in — Subway movesets are mostly high-accuracy, and
// adding it didn't change rank ordering in spot checks.

const STATUS_SCORE_PROTECT = 35;
const STATUS_SCORE_SETUP = 40;
const STATUS_SCORE_DEFAULT = 30;

function statusMoveScore(move) {
    if (move.id === 'protect' || move.id === 'detect' || move.id === 'endure') {
        return STATUS_SCORE_PROTECT;
    }
    if (move.target === 'self' && move.boosts) return STATUS_SCORE_SETUP;
    return STATUS_SCORE_DEFAULT;
}

function damageHeuristic(battle, attacker, move, target) {
    if (!target || target.fainted) return 0;
    if (!move.basePower || move.basePower <= 0) return 0;
    if (battle.dex.getImmunity(move.type, target) === false) return 0;
    const effInt = battle.dex.getEffectiveness(move.type, target);
    const typeMult = Math.pow(2, effInt);
    const stab = attacker.hasType(move.type) ? 1.5 : 1;
    return move.basePower * typeMult * stab;
}

function scoreMoveOnTarget(battle, attacker, side, slotIdx, move, targetStr) {
    if (move.category === 'Status') return statusMoveScore(move);

    // Targeted moves: resolve targetStr to a live Pokemon.
    if (targetStr) {
        let t = null;
        if (targetStr === 'foeSlot0') t = side.foe.active[0];
        else if (targetStr === 'foeSlot1') t = side.foe.active[1];
        else if (targetStr === 'allySlot0' || targetStr === 'allySlot1') {
            // AI avoids damaging its own partner (AI_SCRIPT_DOUBLE_BATTLE).
            return -Infinity;
        }
        return damageHeuristic(battle, attacker, move, t);
    }

    // Spread / self / no-target — score by coverage over foes.
    if (move.target === 'allAdjacentFoes') {
        let sum = 0;
        for (const foe of side.foe.active) sum += damageHeuristic(battle, attacker, move, foe);
        return sum;
    }
    if (move.target === 'allAdjacent') {
        // Hits both foes + partner. Penalize partner hit.
        let sum = 0;
        for (const foe of side.foe.active) sum += damageHeuristic(battle, attacker, move, foe);
        for (let i = 0; i < side.active.length; i++) {
            if (i === slotIdx) continue;
            const ally = side.active[i];
            if (!ally || ally.fainted) continue;
            // Partner damage is treated as a penalty (rough stand-in for AI_SCRIPT_DOUBLE_BATTLE
            // which outright avoids partner-hitting moves when partner is vulnerable).
            sum -= damageHeuristic(battle, attacker, move, ally);
        }
        return sum;
    }
    // Other no-target cases (allies, sides, field). Fall back to status baseline.
    return statusMoveScore(move);
}

function pickGreedySlotMove(battle, side, slotIdx) {
    const pokemon = side.active[slotIdx];
    if (!pokemon || pokemon.fainted) return { type: 'pass' };

    const req = pokemon.getMoveRequestData();
    let bestAction = null;
    let bestScore = -Infinity;

    for (const m of req.moves) {
        if (m.disabled) continue;
        const moveObj = battle.dex.moves.get(m.move);
        const targets = enumerateTargetsForMove(m.target, side, slotIdx);
        for (const t of targets) {
            const score = scoreMoveOnTarget(battle, pokemon, side, slotIdx, moveObj, t);
            if (score > bestScore) {
                bestScore = score;
                bestAction = { type: 'move', move: m.move };
                if (t) bestAction.target = t;
            }
        }
    }

    return bestAction || { type: 'pass' };
}

// Forced switch picker. v1: pick the first alive bench mon for each forced slot.
// Under-supply handled same as enumerateSideSwitchChoices. A smarter heuristic
// (best type matchup vs p1 actives) is a later refinement.
function pickGreedySwitch(side) {
    const req = side.activeRequest;
    if (!req || !req.forceSwitch || !req.forceSwitch.some(Boolean)) return null;

    const benchMons = side.pokemon
        .filter((p, i) => p && !p.fainted && i >= side.active.length)
        .map(p => p.species.name);

    if (!benchMons.length) return null;

    const forceSlotCount = req.forceSwitch.filter(Boolean).length;
    const requiredSwitches = Math.min(benchMons.length, forceSlotCount);

    const picks = [];
    let benchIdx = 0;
    for (let i = 0; i < side.active.length; i++) {
        if (req.forceSwitch[i] && benchIdx < requiredSwitches) {
            picks.push({ type: 'switch', to: benchMons[benchIdx++] });
        } else {
            picks.push({ type: 'pass' });
        }
    }
    return picks;
}

// Compute p2's single greedy action for a battle state (current requestState).
// Returns a per-slot action array compatible with formatSideChoice, or null
// if p2 has no pending request (switch phase where only p1 needs to act).
function pickGreedyP2SideAction(battle) {
    const side = battle.sides[1];
    if (battle.requestState === 'switch') {
        return pickGreedySwitch(side);
    }
    // Move phase: per-slot independent argmax.
    return [pickGreedySlotMove(battle, side, 0), pickGreedySlotMove(battle, side, 1)];
}


// --- Minimax search (ported from src/evaluator.py) ---
//
// Runs the search in-process: each pair branch is `State.deserializeBattle`
// from a parent snapshot + makeChoices + eval. No Python/IPC round-trip per
// pair. Cache is keyed on (state_hash, p1_hash, p2_hash) — PS round-trips the
// PRNG state inside the serialized snapshot so identical inputs are
// deterministic, matching the cache contract in evaluator.py.

const _pairCache = new Map();

// Per-call profile accumulator. Reset at the top of handleBestPairMinimax and
// returned in the response so the Python side can see where time is going.
// Overhead is negligible (~100ns per hrtime read).
const _prof = {
    enumerate_ms: 0,
    serialize_ms: 0,
    clone_ms: 0,
    apply_ms: 0,
    eval_ms: 0,
    pairs_scored: 0,
    cache_hits: 0,
    cache_misses: 0,
    recurse_calls: 0,
    p1_actions_root: 0,
    p2_actions_root: 0,
};

function profReset() {
    for (const k of Object.keys(_prof)) _prof[k] = 0;
}

function hrms(startNs) {
    return Number(process.hrtime.bigint() - startNs) / 1e6;
}

function hashJson(obj) {
    return crypto.createHash('md5').update(JSON.stringify(obj)).digest('hex');
}

function hpFraction(mon) {
    if (mon.fainted) return 0;
    if (!mon.maxhp) return 0;
    return mon.hp / mon.maxhp;
}

function sideHpSum(side) {
    let sum = 0;
    for (const mon of side.pokemon) sum += hpFraction(mon);
    return sum;
}

function evaluateBattle(battle) {
    return sideHpSum(battle.sides[0]) - sideHpSum(battle.sides[1]);
}

function terminalScore(battle) {
    if (!battle.ended) return null;
    // battle.winner is set to side.name ('P1'/'P2') by battle.win(); '' for tie.
    if (battle.winner === battle.sides[0].name) return Infinity;
    if (battle.winner === battle.sides[1].name) return -Infinity;
    return 0;
}

// JSON has no Infinity; encode as strings for wire transport. Python shim
// parses them back to math.inf. Only applied at the response boundary.
function encodeScore(v) {
    if (v === Infinity) return 'inf';
    if (v === -Infinity) return '-inf';
    return v;
}

// p2Policy: 'minimax' enumerates the full p2 action space (adversarial over p2);
// 'greedy' collapses to the single action picked by pickGreedyP2SideAction,
// modeling the real Subway AI as a deterministic function of state. See
// BattleSubwayAI.md for why this is closer to real behavior than adversarial
// minimax (which over-estimates threat).
function enumerateBattlePairs(battle, subwayAiSet, p2Policy) {
    const t0 = process.hrtime.bigint();
    const req = battle.requestState;
    if (req !== 'move' && req !== 'switch') { _prof.enumerate_ms += hrms(t0); return []; }

    let p1Choices, p2Choices;
    if (req === 'switch') {
        p1Choices = enumerateSideSwitchChoices(battle.sides[0]);
        p2Choices = p2Policy === 'greedy'
            ? [pickGreedySwitch(battle.sides[1])]
            : enumerateSideSwitchChoices(battle.sides[1]);
    } else {
        p1Choices = enumerateSideChoices(battle, battle.sides[0], subwayAiSet.has('p1'));
        p2Choices = p2Policy === 'greedy'
            ? [pickGreedyP2SideAction(battle)]
            : enumerateSideChoices(battle, battle.sides[1], subwayAiSet.has('p2'));
    }
    const pairs = [];
    for (const p1 of p1Choices) {
        for (const p2 of p2Choices) pairs.push({ p1, p2 });
    }

    _prof.enumerate_ms += hrms(t0);
    return pairs;
}

function applyChoicesInPlace(battle, pair) {
    const p1Choice = formatSideChoice(pair.p1, battle.sides[0]);
    const p2Choice = formatSideChoice(pair.p2, battle.sides[1]);
    try {
        battle.makeChoices(p1Choice, p2Choice);
    } catch (e) {
        const ctx = {
            requestState: battle.requestState,
            p1Choice, p2Choice,
            pair,
            p1Active: battle.sides[0].active.map(m => m && { name: m.species && m.species.name, fainted: m.fainted }),
            p2Active: battle.sides[1].active.map(m => m && { name: m.species && m.species.name, fainted: m.fainted }),
            p1Request: battle.sides[0].activeRequest,
            p2Request: battle.sides[1].activeRequest,
        };
        e.message += `\n  minimax ctx: ${JSON.stringify(ctx)}`;
        throw e;
    }
}

// Fresh clone of a serialized snapshot — parsing from JSON string each time
// avoids aliasing bugs where deserializeBattle holds refs into the source
// object and mutations across clones bleed through.
function cloneFromJson(snapshotJson) {
    return restoreBattle(JSON.parse(snapshotJson));
}

// Score the result of applying `pair` to the battle represented by `snapshotJson`.
// Cached by (stateHash, p1Hash, p2Hash).
function scorePairCached(snapshotJson, stateHash, pair) {
    const p1Hash = hashJson(pair.p1);
    const p2Hash = hashJson(pair.p2);
    const key = `${stateHash}|${p1Hash}|${p2Hash}`;
    const hit = _pairCache.get(key);
    if (hit !== undefined) { _prof.cache_hits++; return hit; }
    _prof.cache_misses++;
    _prof.pairs_scored++;

    const tc = process.hrtime.bigint();
    const battle = cloneFromJson(snapshotJson);
    _prof.clone_ms += hrms(tc);

    const ta = process.hrtime.bigint();
    applyChoicesInPlace(battle, pair);
    _prof.apply_ms += hrms(ta);

    const te = process.hrtime.bigint();
    const term = terminalScore(battle);
    const score = term !== null ? term : evaluateBattle(battle);
    _prof.eval_ms += hrms(te);

    _pairCache.set(key, score);
    return score;
}

// Group pairs by stringified p1 action — same structure as evaluator.py.
function groupByP1(pairs) {
    const byP1 = new Map();
    for (const pair of pairs) {
        const k = JSON.stringify(pair.p1);
        let list = byP1.get(k);
        if (!list) { list = []; byP1.set(k, list); }
        list.push(pair);
    }
    return byP1;
}

// Adversarial minimax value at the next move phase. Switch phases are
// transparent: for each p1 switch option, minimax over p2 switches, recurse
// into the resulting move phase. Mirrors evaluator.move_phase_value.
function movePhaseValueOfBattle(battle, subwayAiSet, p2Policy) {
    _prof.recurse_calls++;
    const term = terminalScore(battle);
    if (term !== null) return term;
    const req = battle.requestState;
    if (req !== 'move' && req !== 'switch') {
        return evaluateBattle(battle);
    }
    const pairs = enumerateBattlePairs(battle, subwayAiSet, p2Policy);
    if (!pairs.length) return evaluateBattle(battle);

    const { matrix, board } = buildStaticEvalContext(battle);

    const ts = process.hrtime.bigint();
    const snapshotJson = JSON.stringify(State.serializeBattle(battle));
    _prof.serialize_ms += hrms(ts);

    if (req === 'switch') {
        // Recurse through the switch phase into the next move phase.
        // Alpha-beta: max over p1 of (min over p2). Once inner `worst` drops to
        // or below current `best`, this p1 can't beat best — skip remaining p2s.
        let best = -Infinity;
        for (const [, p1Pairs] of groupByP1(pairs)) {
            let worst = Infinity;
            for (const pair of p1Pairs) {
                const tc = process.hrtime.bigint();
                const b = cloneFromJson(snapshotJson);
                _prof.clone_ms += hrms(tc);
                const ta = process.hrtime.bigint();
                applyChoicesInPlace(b, pair);
                _prof.apply_ms += hrms(ta);
                _prof.pairs_scored++;
                const t = terminalScore(b);
                const v = t !== null ? t : movePhaseValueOfBattle(b, subwayAiSet, p2Policy, matrix, board);
                if (v < worst) worst = v;
                if (worst <= best) break; // prune: this p1 branch is dominated
            }
            if (worst > best) best = worst;
        }
        return best;
    }

    // Move phase: depth-1 minimax with alpha-beta pruning on the outer max.
    let best = -Infinity;
    for (const [, p1Pairs] of groupByP1(pairs)) {
        let worst = Infinity;
        for (const pair of p1Pairs) {
            _prof.pairs_scored++;
            const s = staticScorePair(matrix, board, pair, battle);
            if (s < worst) worst = s;
            if (worst <= best) break; // prune: this p1 branch is dominated
        }
        if (worst > best) best = worst;
    }
    return best;
}

// Depth-1 adversarial minimax, scored list.
// Returns [{p1, d1, adv_p2}, ...] sorted by d1 desc.
//function minimaxScoredFromSnapshot(snapshotJson, stateHash, pairs) {
function minimaxScoredFromSnapshot(pairs, matrix, board, battle) {
    const out = [];
    for (const [, p1Pairs] of groupByP1(pairs)) {
        // Default worstPair to the first candidate so we always have an
        // adv_p2 to report, even when all responses evaluate to +Infinity
        // (every p2 reply lets p1 win outright — common under greedy p2).
        let worstPair = p1Pairs[0];
        let worstScore = Infinity;
        for (const pair of p1Pairs) {
            //const s = scorePairCached(snapshotJson, stateHash, pair);
            const s = staticScorePair(matrix, board, pair, battle)
            if (s < worstScore) {
                worstScore = s;
                worstPair = pair;
            }
        }
        out.push({ p1: p1Pairs[0].p1, d1: worstScore, adv_p2: worstPair.p2 });
    }
    out.sort((a, b) => b.d1 - a.d1);
    return out;
}

function handleBestPairMinimax(params) {
    profReset();
    const tTotal = process.hrtime.bigint();

    const { state, depth = 2, top_k = 5, subway_ai_sides = [], p2_policy = 'minimax' } = params;
    if (!state) throw new Error('best_pair_minimax requires state');
    if (depth !== 1 && depth !== 2) throw new Error(`unsupported depth: ${depth}`);
    if (p2_policy !== 'minimax' && p2_policy !== 'greedy') {
        throw new Error(`unsupported p2_policy: ${p2_policy}`);
    }

    const subwayAiSet = new Set(subway_ai_sides);
    const battle = restoreBattle(state);

    const { matrix, board } = buildStaticEvalContext(battle, forceDamageRoll);

    // Canonical snapshot: re-serialize so its hash matches subsequent clones.
    // (The input `state` may have been mutated by the caller; belt-and-braces.)
    const ts = process.hrtime.bigint();
    const snapshotJson = JSON.stringify(State.serializeBattle(battle));
    _prof.serialize_ms += hrms(ts);
    const stateHash = crypto.createHash('md5').update(snapshotJson).digest('hex');

    const pairs = enumerateBattlePairs(battle, subwayAiSet, p2_policy);
    if (!pairs.length) {
        return {
            chosen_pair: null, scored: [], depth, cache_entries: _pairCache.size,
            profile: { ..._prof, total_ms: hrms(tTotal) },
        };
    }

    // Root branching factor — useful for spotting blowups.
    _prof.p1_actions_root = new Set(pairs.map(p => JSON.stringify(p.p1))).size;
    _prof.p2_actions_root = new Set(pairs.map(p => JSON.stringify(p.p2))).size;

    //const d1Scored = minimaxScoredFromSnapshot(snapshotJson, stateHash, pairs);
    const d1Scored = minimaxScoredFromSnapshot(pairs, matrix, board, battle);

    if (depth === 1) {
        const top = d1Scored[0];
        return {
            chosen_pair: { p1: top.p1, p2: top.adv_p2 },
            scored: d1Scored.map(r => ({
                p1: r.p1, d1: encodeScore(r.d1), d2: encodeScore(r.d1), adv_p2: r.adv_p2,
            })),
            depth: 1,
            cache_entries: _pairCache.size,
            profile: { ..._prof, total_ms: hrms(tTotal) },
        };
    }

    // depth === 2: top-K expansion. Fixed-b1 approximation (see evaluator.py).
    const top = d1Scored.slice(0, top_k);
    const results = [];
    for (const row of top) {
        if (!isFinite(row.d1)) {
            results.push({ p1: row.p1, d1: row.d1, d2: row.d1, adv_p2: row.adv_p2 });
            continue;
        }
        const tc = process.hrtime.bigint();
        const b = cloneFromJson(snapshotJson);
        _prof.clone_ms += hrms(tc);
        const ta = process.hrtime.bigint();
        applyChoicesInPlace(b, { p1: row.p1, p2: row.adv_p2 });
        _prof.apply_ms += hrms(ta);
        _prof.pairs_scored++;
        const t = terminalScore(b);
        if (t !== null) {
            results.push({ p1: row.p1, d1: row.d1, d2: t, adv_p2: row.adv_p2 });
            continue;
        }
        const d2 = movePhaseValueOfBattle(b, subwayAiSet, p2_policy, matrix, board);
        results.push({ p1: row.p1, d1: row.d1, d2, adv_p2: row.adv_p2 });
    }
    results.sort((a, b) => b.d2 - a.d2);
    const best = results[0];
    return {
        chosen_pair: { p1: best.p1, p2: best.adv_p2 },
        scored: results.map(r => ({
            p1: r.p1, d1: encodeScore(r.d1), d2: encodeScore(r.d2), adv_p2: r.adv_p2,
        })),
        depth: 2,
        cache_entries: _pairCache.size,
        profile: { ..._prof, total_ms: hrms(tTotal) },
    };
}

function handleClearMinimaxCache() {
    const n = _pairCache.size;
    _pairCache.clear();
    return { cleared: n };
}

function handleMinimaxCacheStats() {
    return { entries: _pairCache.size };
}


function handleEnumerateTurn(params) {
    const { state, subway_ai_sides } = params;
    if (!state) throw new Error('enumerate_turn requires state');
    const battle = restoreBattle(state);

    if (battle.requestState !== 'move' && battle.requestState !== 'switch') {
        return {
            requestState: battle.requestState,
            note: `enumerate_turn supports requestState in ('move', 'switch'); got '${battle.requestState}'`,
            action_pairs: [],
        };
    }

    const subwayAiSet = new Set(subway_ai_sides || []);
    const p1Subway = subwayAiSet.has('p1');
    const p2Subway = subwayAiSet.has('p2');

    let p1Choices, p2Choices;
    if (battle.requestState === 'switch') {
        p1Choices = enumerateSideSwitchChoices(battle.sides[0]);
        p2Choices = enumerateSideSwitchChoices(battle.sides[1]);
    } else {
        p1Choices = enumerateSideChoices(battle, battle.sides[0], p1Subway);
        p2Choices = enumerateSideChoices(battle, battle.sides[1], p2Subway);
    }

    const pairs = [];
    for (const p1 of p1Choices) {
        for (const p2 of p2Choices) {
            pairs.push({ p1, p2 });
        }
    }
    return {
        requestState: battle.requestState,
        p1_choice_count: p1Choices.length,
        p2_choice_count: p2Choices.length,
        action_pairs: pairs,
    };
}


const handlers = {
    start_battle: handleStartBattle,
    execute_turn: handleExecuteTurn,
    damage_range: handleDamageRange,
    enumerate_turn: handleEnumerateTurn,
    best_pair_minimax: handleBestPairMinimax,
    clear_minimax_cache: handleClearMinimaxCache,
    minimax_cache_stats: handleMinimaxCacheStats,
};


// --- stdin/stdout loop ---

const rl = readline.createInterface({ input: process.stdin, terminal: false });

rl.on('line', (line) => {
    line = line.trim();
    if (!line) return;

    let req;
    try {
        req = JSON.parse(line);
    } catch (e) {
        process.stdout.write(JSON.stringify({ id: null, error: `parse error: ${e.message}` }) + '\n');
        return;
    }

    const { id = null, method, params } = req;
    const handler = handlers[method];
    if (!handler) {
        process.stdout.write(JSON.stringify({ id, error: `unknown method: ${method}` }) + '\n');
        return;
    }

    try {
        const result = handler(params || {});
        process.stdout.write(JSON.stringify({ id, result }) + '\n');
    } catch (e) {
        process.stdout.write(JSON.stringify({ id, error: `${e.message}\n${e.stack}` }) + '\n');
    }
});

rl.on('close', () => { process.exit(0); });
