/**
 * static_eval.js
 *
 * Static depth-1 pair evaluator. Replaces clone+makeChoices+evaluateBattle
 * for the depth-1 scoring loop in handleBestPairMinimax.
 *
 * Design:
 *   - Build a DamageMatrix once per battle state (before the search loop)
 *   - staticScorePair() uses that matrix + speed order to estimate post-turn
 *     HP fractions without running any PS simulation
 *   - Uses MIN damage for p1 actions (pessimistic), MAX for p2 (worst-case)
 *   - Cancels a fainted mon's action if it is KO'd before it moves
 *   - Returns the same scalar as evaluateBattle(): p1_hp_sum - p2_hp_sum
 *
 * Limitations (acceptable for ranking purposes):
 *   - No secondary effects (burn, para, flinch)
 *   - No stat stage changes within the turn
 *   - No spread move reduction yet (add when handleDamageRange covers it)
 *   - No multi-hit moves
 *   - No ability triggers mid-turn (Intimidate on switch etc.)
 *   - Speed ties broken by slot order (slot0 > slot1), matches PS default
 *
 * Integration:
 *   Replace scorePairCached() calls in the depth-1 loop with
 *   staticScorePair(matrix, boardSnap, pair).
 *   Keep cloneFromJson+applyChoicesInPlace only for the top-k depth-2
 *   expansions where you need the actual resulting PS battle state.
 */

'use strict';

// ---------------------------------------------------------------------------
// Priority tiers (Gen 5). Extend as needed for moves in the Subway pool.
// Source: Bulbapedia "Priority" — only non-zero entries listed.
// ---------------------------------------------------------------------------
const MOVE_PRIORITY = {
    'helpinghand': 5,
    'protect': 4, 'detect': 4, 'endure': 4, 'wideguard': 4, 'quickguard': 4,
    'fakeout': 3, 'followme': 3, 'ragepowder': 3,
    'quickattack': 1, 'machpunch': 1, 'bulletpunch': 1, 'iceshard': 1,
    'shadowsneak': 1, 'aquajet': 1, 'suckerpunch': 1, 'extremespeed': 1,
    'vacuumwave': 1, 'watershuriken': 1,
    'vitalthrow': -1,
    'focuspunch': -3,
    'avalanche': -4, 'revenge': -4,
    'counter': -5, 'mirrorcoat': -5,
    'roar': -6, 'whirlwind': -6, 'dragontail': -6, 'circlethrow': -6,
    'trickroom': -7,
};

// Moves that deal 0 damage to opponents (status, self-targeting, etc.)
// staticScorePair skips damage lookup for these.
const NON_DAMAGING = new Set([
    'protect', 'detect', 'endure', 'wideguard', 'quickguard',
    'tailwind', 'sunnyday', 'raindance', 'sandstorm', 'hail',
    'trickroom', 'helpinghand', 'followme', 'ragepowder',
    'substitute', 'swordsdance', 'nastyplot', 'calmmind', 'dragondance',
    'barrier', 'amnesia', 'irondefense', 'cosmicpower', 'acidarmor',
    'agility', 'rockpolish', 'quiverdance', 'shellsmash',
    'recover', 'roost', 'moonlight', 'morningsun', 'synthesis',
    'rest', 'sleeptalk', 'safeguard', 'lightscreen', 'reflect',
    'taunt', 'encore', 'disable', 'torment', 'swagger', 'flatter',
    'willowisp', 'thunderwave', 'toxic', 'spore', 'sleeppowder',
    'stunspore', 'glare', 'hypnosis', 'sing', 'grasswhistle', 'yawn',
    'leechseed', 'ingrain', 'aquaring', 'magnetrise',
    'batonpass', 'uturn', 'voltswitch',
]);

// ---------------------------------------------------------------------------
// DamageMatrix
// ---------------------------------------------------------------------------

/**
 * Slot descriptor extracted from a live battle for static evaluation.
 * All fields are plain numbers — no PS object references.
 *
 * @typedef {Object} SlotSnap
 * @property {string}  key        - Unique key: "p1s0", "p2s1" etc.
 * @property {string}  side       - "p1" | "p2"
 * @property {number}  slot       - 0 or 1
 * @property {number}  hp         - Current HP (raw)
 * @property {number}  maxhp      - Max HP (raw)
 * @property {number}  hpFrac     - hp / maxhp
 * @property {number}  spe        - Effective speed stat (after stage modifiers)
 * @property {boolean} fainted    - true if hp === 0
 * @property {string[]} moves     - Move names (lowercase)
 * @property {string}  ability    - Ability id (lowercase)
 * @property {string}  item       - Item id (lowercase)
 * @property {boolean} prankster  - Cached ability === 'prankster'
 * @property {number}  tailwindMult - 2 if tailwind is up for this side, else 1
 */

/**
 * Build a SlotSnap from a live PS Battle + side index + slot index.
 * Call this once before the search loop.
 *
 * @param {Battle} battle
 * @param {number} sideIdx  0 = p1, 1 = p2
 * @param {number} slot     0 or 1
 * @returns {SlotSnap|null}
 */
function buildSlotSnap(battle, sideIdx, slot) {
    const side = battle.sides[sideIdx];
    const mon = side.active[slot];
    if (!mon || mon.fainted) return null;

    // Effective speed: base stat × nature × EV/IV modifier × stage multiplier.
    // PS stores the final stat in mon.stats.spe; stage is in mon.boosts.spe.
    const stageMult = (stage) => {
        if (stage >= 0) return (2 + stage) / 2;
        return 2 / (2 - stage);
    };
    const rawSpe = mon.stats ? mon.stats.spe : 0;
    const speStage = (mon.boosts && mon.boosts.spe) ? mon.boosts.spe : 0;
    let spe = Math.floor(rawSpe * stageMult(speStage));

    // Tailwind doubles speed.
    const tailwindActive = side.sideConditions && side.sideConditions['tailwind'];
    if (tailwindActive) spe *= 2;

    // Paralysis halves speed (Gen 5: ÷4 in later gens but ÷2 in Gen 5 PS).
    if (mon.status === 'par') spe = Math.floor(spe / 2);

    const ability = (mon.ability || '').toLowerCase();
    const item = (mon.item || '').toLowerCase();

    return {
        key: `p${sideIdx + 1}s${slot}`,
        side: sideIdx === 0 ? 'p1' : 'p2',
        slot,
        hp: mon.hp,
        maxhp: mon.maxhp,
        hpFrac: mon.maxhp > 0 ? mon.hp / mon.maxhp : 0,
        spe,
        fainted: false,
        moves: (mon.moves || []).map(m => (typeof m === 'string' ? m : m.id || '').toLowerCase()),
        ability,
        item,
        prankster: ability === 'prankster',
        tailwindMult: tailwindActive ? 2 : 1,
        atkStage: (mon.boosts && mon.boosts.atk) || 0,
        spaStage: (mon.boosts && mon.boosts.spa) || 0,
    };
}

// Gen 5 stage multiplier for atk/spa/def/spd. Symmetric with stageMult in buildSlotSnap.
function stageMult(stage) {
    if (stage >= 0) return (2 + stage) / 2;
    return 2 / (2 - stage);
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


/**
 * @typedef {Object} DamageEntry
 * @property {number} min       - Min raw damage (roll 15/15)
 * @property {number} max       - Max damage (roll 0/15)
 * @property {number} defMaxHp  - Defender max HP
 * @property {number} accuracy  - 0–1
 * @property {boolean} ohkoMin  - min damage >= defMaxHp && accuracy === 1
 * @property {boolean} ohkoMax  - max damage >= defMaxHp
 */

/**
 * DamageMatrix: nested map  attacker_key → move_name → defender_key → DamageEntry
 *
 * Build once per matchup state. Requires the PS getDamage helper already
 * used by handleDamageRange — pass the battle object directly.
 *
 * @param {Battle} battle       - Live PS battle (will not be mutated)
 * @param {SlotSnap[]} snaps    - All 4 slot snaps (p1s0, p1s1, p2s0, p2s1)
 * @returns {Map}
 */
function buildDamageMatrix(battle, snaps) {
    const matrix = new Map(); // attackerKey → Map(moveName → Map(defenderKey → DamageEntry))

    for (const atkSnap of snaps) {
        if (!atkSnap) continue;
        const atkSideIdx = atkSnap.side === 'p1' ? 0 : 1;
        const atkMon = battle.sides[atkSideIdx].active[atkSnap.slot];
        if (!atkMon || atkMon.fainted) continue;

        const moveMap = new Map();
        matrix.set(atkSnap.key, moveMap);

        for (const moveName of atkSnap.moves) {
            if (NON_DAMAGING.has(moveName)) continue;

            const defMap = new Map();
            moveMap.set(moveName, defMap);

            // Compute damage against every opposing active slot.
            const defSideIdx = 1 - atkSideIdx;
            const defSide = battle.sides[defSideIdx];

            for (let defSlot = 0; defSlot < 2; defSlot++) {
                const defMon = defSide.active[defSlot];
                if (!defMon || defMon.fainted) continue;
                const defSnap = snaps.find(s => s && s.side !== atkSnap.side && s.slot === defSlot);
                if (!defSnap) continue;

                // Use the same getDamage calls as handleDamageRange.
                // forceDamageRoll is assumed to be available in scope (same file as driver.js).
                let maxDmg, minDmg;
                try {
                    forceDamageRoll(battle, 0);
                    maxDmg = battle.actions.getDamage(atkMon, defMon, moveName, true);
                    forceDamageRoll(battle, 15);
                    minDmg = battle.actions.getDamage(atkMon, defMon, moveName, true);
                } catch (_) {
                    // Move not applicable (e.g. wrong target type) — skip.
                    continue;
                }

                if (typeof maxDmg !== 'number' || typeof minDmg !== 'number') continue;

                const defMaxHp = defMon.maxhp;
                const moveObj = battle.dex.moves.get(moveName);
                const accuracy = !moveObj || moveObj.accuracy === true
                    ? 1.0
                    : moveObj.accuracy / 100;

                // Apply item accuracy modifiers.
                const atkSnapItem = slugify(atkSnap.item);
                const finalAccuracy = applyAccuracyItem(accuracy, atkSnapItem, moveName);

                // Spread flag: moves that hit multiple adjacent targets get a
                // 0.75× damage multiplier per hit. PS applies this in the full
                // damage pipeline but not in getDamage(), so we apply it here.
                const spreadTargets = new Set([
                    'allAdjacent', 'allAdjacentFoes',
                ]);
                const isSpread = moveObj && spreadTargets.has(moveObj.target);

                defMap.set(defSnap.key, {
                    min: minDmg,
                    max: maxDmg,
                    defMaxHp,
                    accuracy: finalAccuracy,
                    isSpread,
                    ohkoMin: minDmg >= defMaxHp && finalAccuracy === 1.0,
                    ohkoMax: maxDmg >= defMaxHp,
                });
            }
        }
    }

    return matrix;
}

/**
 * Apply known item accuracy modifiers.
 * Extend as new items appear in the Subway pool.
 */
function applyAccuracyItem(baseAccuracy, itemId, moveName) {
    if (itemId === 'widelens') return Math.min(1.0, baseAccuracy * 1.1);
    if (itemId === 'zoomlens') return Math.min(1.0, baseAccuracy * 1.2); // if moving last
    return baseAccuracy;
}

// ---------------------------------------------------------------------------
// BoardSnapshot
// ---------------------------------------------------------------------------

/**
 * Lightweight snapshot of the current board state needed by staticScorePair.
 * Extract once before the search loop and pass alongside the damage matrix.
 *
 * @typedef {Object} BoardSnap
 * @property {SlotSnap|null} p1s0
 * @property {SlotSnap|null} p1s1
 * @property {SlotSnap|null} p2s0
 * @property {SlotSnap|null} p2s1
 * @property {boolean} trickRoom  - Whether Trick Room is currently active
 */

/**
 * @param {Battle} battle
 * @returns {BoardSnap}
 */
function buildBoardSnap(battle) {
    const snaps = [
        buildSlotSnap(battle, 0, 0),
        buildSlotSnap(battle, 0, 1),
        buildSlotSnap(battle, 1, 0),
        buildSlotSnap(battle, 1, 1),
    ];
    const trickRoom = !!(battle.field && battle.field.pseudoWeather && battle.field.pseudoWeather['trickroom']);
    // Beat Up hits = count of non-fainted party members on the attacker's side.
    const partyAlive = [0, 1].map(i => battle.sides[i].pokemon.filter(p => p && !p.fainted).length);
    // Bench HP: mons not currently active. Doesn't change mid-turn unless a
    // switch happens; we approximate by pre-computing once and re-applying
    // inside staticScorePair when a slot switches out/in.
    const benchByKey = [{}, {}];
    for (let sideIdx = 0; sideIdx < 2; sideIdx++) {
        const side = battle.sides[sideIdx];
        for (const p of side.pokemon) {
            if (!p) continue;
            if (side.active.includes(p)) continue;
            const name = p.species && p.species.name;
            const frac = p.fainted || !p.maxhp ? 0 : p.hp / p.maxhp;
            if (name) benchByKey[sideIdx][name] = frac;
        }
    }
    // Also store the active-HP by species name so a switch-out can restore
    // that mon's current HP when it goes to bench. buildSlotSnap gives it,
    // but we need by-name lookup for switch actions.
    const activeByName = [{}, {}];
    for (let sideIdx = 0; sideIdx < 2; sideIdx++) {
        for (const mon of battle.sides[sideIdx].active) {
            if (!mon) continue;
            const name = mon.species && mon.species.name;
            if (!name) continue;
            const frac = mon.fainted || !mon.maxhp ? 0 : mon.hp / mon.maxhp;
            activeByName[sideIdx][name] = frac;
        }
    }
    return {
        p1s0: snaps[0], p1s1: snaps[1], p2s0: snaps[2], p2s1: snaps[3],
        trickRoom,
        p1PartyAlive: partyAlive[0],
        p2PartyAlive: partyAlive[1],
        p1Bench: benchByKey[0],
        p2Bench: benchByKey[1],
        p1ActiveHp: activeByName[0],
        p2ActiveHp: activeByName[1],
    };
}

// ---------------------------------------------------------------------------
// Action → (move, target) extraction
// ---------------------------------------------------------------------------

/**
 * Extract the relevant move name and target slot from a single action object.
 * Returns null for pass/switch actions (no damage contribution).
 *
 * @param {Object} action  - e.g. {type:'move', move:'Rock Slide', target:'foeSlot0'}
 * @returns {{moveName: string, targetSlot: number}|null}
 */

function slugify(name) {
    return (name || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

function extractMoveTarget(action) {
    if (!action || action.type !== 'move') return null;
    const moveName = slugify(action.move);

    if (NON_DAMAGING.has(moveName)) return null;

    const allyMatch = action.target && action.target.match(/allySlot(\d)/);
    const foeMatch = action.target && action.target.match(/foeSlot(\d)/);

    return {
        moveName,
        foeSlot: foeMatch ? parseInt(foeMatch[1], 10) : null,
        allySlot: allyMatch ? parseInt(allyMatch[1], 10) : null,
        isSpread: !action.target || action.target === 'normal',
    };
}

// Moves that proc Justified on ally via type, even if this file doesn't
// track per-move type. We rely on the damage matrix entry having been built
// (which implicitly encodes type effectiveness), but for ability triggers
// we need the move's actual type. Pulled from battle.dex.
function moveTypeId(battle, moveName) {
    const mv = battle.dex.moves.get(moveName);
    return mv && mv.type ? mv.type : '';
}

function moveCategoryPhysical(battle, moveName) {
    const mv = battle.dex.moves.get(moveName);
    return mv && mv.category === 'Physical';
}

/**
 * Determine the effective priority of an action for a given slot.
 * Prankster adds +1 to status moves; standard otherwise.
 *
 * @param {Object} action
 * @param {SlotSnap} atkSnap
 * @param {Battle} battle  - needed for move category lookup
 * @returns {number}
 */
function effectivePriority(action, atkSnap, battle) {
    if (!action || action.type !== 'move') return 0;
    const moveName = (action.move || '').toLowerCase();
    const base = MOVE_PRIORITY[moveName] || 0;
    if (atkSnap && atkSnap.prankster) {
        const moveObj = battle.dex.moves.get(moveName);
        if (moveObj && moveObj.category === 'Status') return base + 1;
    }
    return base;
}

// ---------------------------------------------------------------------------
// Speed order resolution
// ---------------------------------------------------------------------------

/**
 * One actor in the turn order.
 * @typedef {Object} TurnActor
 * @property {string}   key       - SlotSnap key e.g. "p1s0"
 * @property {string}   side      - "p1" | "p2"
 * @property {number}   slot
 * @property {number}   priority
 * @property {number}   spe
 * @property {Object}   action    - Raw action object
 * @property {SlotSnap} snap
 */

/**
 * Resolve the turn order for a pair of side-actions.
 * Returns actors sorted by (priority DESC, speed DESC [or ASC under TR], tie→p1 first).
 *
 * Handles:
 * - Standard priority tiers
 * - Prankster +1 on status moves
 * - Trick Room speed inversion
 * - Tailwind (already baked into snap.spe)
 * - Pass actions (no-op, placed last)
 *
 * @param {Object[]} p1Actions   - Array of 2 action objects for p1 slots
 * @param {Object[]} p2Actions   - Array of 2 action objects for p2 slots
 * @param {BoardSnap} board
 * @param {Battle} battle
 * @returns {TurnActor[]}
 */
function resolveTurnOrder(p1Actions, p2Actions, board, battle) {
    const actors = [];

    const addActor = (action, snap, side, slot) => {
        if (!snap || snap.fainted) return;
        if (!action || action.type === 'pass') return;
        const priority = effectivePriority(action, snap, battle);
        actors.push({ key: snap.key, side, slot, priority, spe: snap.spe, action, snap });
    };

    addActor(p1Actions[0], board.p1s0, 'p1', 0);
    addActor(p1Actions[1], board.p1s1, 'p1', 1);
    addActor(p2Actions[0], board.p2s0, 'p2', 0);
    addActor(p2Actions[1], board.p2s1, 'p2', 1);

    actors.sort((a, b) => {
        // 1. Priority tier (always descending regardless of TR).
        if (b.priority !== a.priority) return b.priority - a.priority;
        // 2. Speed (descending normally, ascending under Trick Room).
        if (a.spe !== b.spe) {
            return board.trickRoom ? a.spe - b.spe : b.spe - a.spe;
        }
        // 3. Tie-break: p1 before p2 (slot 0 before slot 1 within same side).
        if (a.side !== b.side) return a.side === 'p1' ? -1 : 1;
        return a.slot - b.slot;
    });

    return actors;
}

// ---------------------------------------------------------------------------
// staticScorePair
// ---------------------------------------------------------------------------

/**
 * Estimate the post-turn HP-fraction delta (p1_sum - p2_sum) for a given
 * action pair without running any PS simulation.
 *
 * Uses:
 *   - MIN damage for p1 actions (pessimistic for p1 → conservative ranking)
 *   - MAX damage for p2 actions (worst-case opponent)
 *   - Correct turn order via resolveTurnOrder()
 *   - Fainted-mon action cancellation: if a mon is KO'd before it acts,
 *     its action is skipped
 *
 * @param {Map}       matrix   - DamageMatrix from buildDamageMatrix()
 * @param {BoardSnap} board    - BoardSnap from buildBoardSnap()
 * @param {Object}    pair     - {p1: [action0, action1], p2: [action0, action1]}
 * @param {Battle}    battle   - Needed only for priority/category lookups (not mutated)
 * @returns {number}  score in same units as evaluateBattle()
 */
function staticScorePair(matrix, board, pair, battle) {
    const p1Actions = Array.isArray(pair.p1) ? pair.p1 : [pair.p1];
    const p2Actions = Array.isArray(pair.p2) ? pair.p2 : [pair.p2];

    // Identify switching slots up-front so hp[] can be seeded with the
    // incoming mon's bench HP (rather than the leaving mon's active HP).
    const switchingTo = {}; // slotKey -> incoming species name
    for (const [actions, sideKey] of [[p1Actions, 'p1'], [p2Actions, 'p2']]) {
        for (let slotIdx = 0; slotIdx < 2; slotIdx++) {
            const a = actions[slotIdx];
            if (a && a.type === 'switch' && a.to) {
                switchingTo[`${sideKey}s${slotIdx}`] = a.to;
            }
        }
    }

    // Mutable HP state: track current HP as fractions during the turn.
    const hp = {};
    // Mutable atk-stage delta applied during this turn (Justified / Intimidate).
    const atkDelta = {};
    const snapByKey = {};
    for (const snap of [board.p1s0, board.p1s1, board.p2s0, board.p2s1]) {
        if (!snap) continue;
        const key = snap.key;
        snapByKey[key] = snap;
        if (switchingTo[key]) {
            // Slot is switching — seed with the incoming bench mon's HP so
            // same-turn incoming damage to this slot reduces that mon's HP.
            const benchMap = key.startsWith('p1') ? board.p1Bench : board.p2Bench;
            hp[key] = benchMap[switchingTo[key]] ?? 0;
        } else {
            hp[key] = snap.hpFrac;
        }
    }

    const fainted = (key) => hp[key] !== undefined && hp[key] <= 0;

    // Intimidate on switch-in: if a side switches in a mon with Intimidate,
    // the opposing side's active mons lose 1 atk stage for this turn's damage
    // calc. Switches happen before moves (priority = 6), so boost applies to
    // all same-turn attacks from the foes. We don't have the incoming mon's
    // ability in the action, so match on name via battle.sides.
    const applyIntimidate = (sideName, toName) => {
        const sideIdx = sideName === 'p1' ? 0 : 1;
        const foeIdx = 1 - sideIdx;
        const incoming = battle.sides[sideIdx].pokemon.find(p => p && p.species && p.species.name === toName);
        if (!incoming || (incoming.ability || '').toLowerCase() !== 'intimidate') return;
        for (let s = 0; s < 2; s++) {
            const foeKey = `p${foeIdx + 1}s${s}`;
            if (snapByKey[foeKey] && !fainted(foeKey)) {
                atkDelta[foeKey] = (atkDelta[foeKey] || 0) - 1;
            }
        }
    };
    for (const [actions, side] of [[p1Actions, 'p1'], [p2Actions, 'p2']]) {
        for (const a of actions) {
            if (a && a.type === 'switch' && a.to) applyIntimidate(side, a.to);
        }
    }

    // Physical damage scaling from a mid-turn atk boost/drop. Ratio of
    // (2 + baseStage + delta) / (2 + baseStage), floored at zero.
    const atkScaleFor = (key) => {
        const d = atkDelta[key] || 0;
        if (d === 0) return 1;
        const snap = snapByKey[key];
        if (!snap) return 1;
        const base = snap.atkStage;
        const cur = Math.max(-6, Math.min(6, base + d));
        return stageMult(cur) / stageMult(base);
    };

    const order = resolveTurnOrder(p1Actions, p2Actions, board, battle);

    for (const actor of order) {
        if (fainted(actor.key)) continue;

        const mt = extractMoveTarget(actor.action);
        if (!mt) continue; // protect/status/pass — no damage

        const atkKey = actor.key;
        const isP1 = actor.side === 'p1';
        const allySide = actor.side;
        const foeSide = isP1 ? 'p2' : 'p1';

        // Ally-targeted move: no HP swing on opponents, but may proc abilities
        // on the ally. Currently model: Justified on ally from Dark moves
        // (including Beat Up, which hits N times where N = healthy party).
        if (mt.allySlot !== null) {
            const allyKey = `${allySide}s${mt.allySlot}`;
            const allySnap = snapByKey[allyKey];
            if (!allySnap || fainted(allyKey)) continue;
            if (allySnap.ability === 'justified' && moveTypeId(battle, mt.moveName) === 'Dark') {
                const partyAlive = allySide === 'p1' ? board.p1PartyAlive : board.p2PartyAlive;
                const hits = mt.moveName === 'beatup' ? Math.max(1, partyAlive) : 1;
                atkDelta[allyKey] = (atkDelta[allyKey] || 0) + hits;
            }
            continue;
        }

        const moveMap = matrix.get(atkKey);
        if (!moveMap) continue;
        const defMap = moveMap.get(mt.moveName);
        if (!defMap) continue;

        const isPhysical = moveCategoryPhysical(battle, mt.moveName);
        const scale = isPhysical ? atkScaleFor(atkKey) : 1;

        // Spread-multiplier: any entry in defMap for this move carries isSpread.
        // 0.75x applies per hit iff the spread actually strikes 2+ live foes.
        const foeSlotsAlive = [0, 1].filter(s => !fainted(`${foeSide}s${s}`)).length;
        const sampleEntry = defMap.values().next().value;
        const spreadMult = (sampleEntry && sampleEntry.isSpread && foeSlotsAlive >= 2) ? 0.75 : 1;

        for (const [defKey, entry] of defMap.entries()) {
            if (mt.foeSlot !== null) {
                const expected = `${foeSide}s${mt.foeSlot}`;
                if (defKey !== expected) continue;
            }
            if (fainted(defKey)) continue;

            // Expected damage: mean roll × accuracy. Symmetric across sides —
            // the adversarial worst-case is captured by minimax over actions,
            // not by inflating per-action RNG pessimism.
            const meanRaw = (entry.min + entry.max) / 2;
            const rawDmg = meanRaw * entry.accuracy * scale * spreadMult;
            // Beat Up: matrix stores one-hit damage; real hits = party alive.
            const hitMult = mt.moveName === 'beatup'
                ? Math.max(1, allySide === 'p1' ? board.p1PartyAlive : board.p2PartyAlive)
                : 1;

            const defSnap = snapByKey[defKey];
            if (!defSnap) continue;
            const dmgFrac = defSnap.maxhp > 0 ? (rawDmg * hitMult) / defSnap.maxhp : 0;
            hp[defKey] = Math.max(0, hp[defKey] - dmgFrac);
        }
    }

    // Actives post-turn.
    let p1Sum = 0, p2Sum = 0;
    for (const snap of [board.p1s0, board.p1s1].filter(Boolean)) p1Sum += Math.max(0, hp[snap.key] ?? 0);
    for (const snap of [board.p2s0, board.p2s1].filter(Boolean)) p2Sum += Math.max(0, hp[snap.key] ?? 0);

    // Bench total (pre-turn; bench HP doesn't change mid-turn).
    let p1Bench = 0, p2Bench = 0;
    for (const v of Object.values(board.p1Bench)) p1Bench += v;
    for (const v of Object.values(board.p2Bench)) p2Bench += v;

    // Switch swap: outgoing mon rejoins bench at its pre-turn active HP;
    // incoming mon leaves bench to occupy the active slot (its HP was
    // already seeded into hp[slotKey] above).
    for (const [actions, sideKey] of [[p1Actions, 'p1'], [p2Actions, 'p2']]) {
        for (let slotIdx = 0; slotIdx < 2; slotIdx++) {
            const a = actions[slotIdx];
            if (!a || a.type !== 'switch' || !a.to) continue;
            const slotKey = `${sideKey}s${slotIdx}`;
            const preActiveHp = snapByKey[slotKey] ? snapByKey[slotKey].hpFrac : 0;
            const benchMap = sideKey === 'p1' ? board.p1Bench : board.p2Bench;
            const incomingBenchHp = benchMap[a.to] ?? 0;
            const delta = preActiveHp - incomingBenchHp;
            if (sideKey === 'p1') p1Bench += delta;
            else p2Bench += delta;
        }
    }

    return (p1Sum + p1Bench) - (p2Sum + p2Bench);
}

// ---------------------------------------------------------------------------
// Integration helpers
// ---------------------------------------------------------------------------

/**
 * Build both the DamageMatrix and BoardSnap from a live battle.
 * Call once before the depth-1 scoring loop.
 *
 * @param {Battle} battle
 * @returns {{ matrix: Map, board: BoardSnap, snaps: SlotSnap[] }}
 */
function buildStaticEvalContext(battle) {
    const snaps = [
        buildSlotSnap(battle, 0, 0),
        buildSlotSnap(battle, 0, 1),
        buildSlotSnap(battle, 1, 0),
        buildSlotSnap(battle, 1, 1),
    ].filter(Boolean);

    const board = buildBoardSnap(battle);
    const matrix = buildDamageMatrix(battle, snaps);
    return { matrix, board, snaps };
}

/**
 * Drop-in replacement for the depth-1 scoring loop in minimaxScoredFromSnapshot.
 *
 * Usage in handleBestPairMinimax:
 *
 *   // Before the loop:
 *   const { matrix, board } = buildStaticEvalContext(battle);
 *
 *   // Replace scorePairCached(snapshotJson, stateHash, pair) with:
 *   staticScorePair(matrix, board, pair, battle);
 *
 * The full clone+makeChoices path is kept only for the top-k depth-2
 * expansions where you need the actual resulting PS state.
 */

module.exports = {
    buildStaticEvalContext,
    staticScorePair,
    // Exported for testing / extension:
    buildDamageMatrix,
    buildBoardSnap,
    buildSlotSnap,
    resolveTurnOrder,
    effectivePriority,
    MOVE_PRIORITY,
    NON_DAMAGING,
};
