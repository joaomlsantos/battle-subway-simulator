// Thin Node.js driver that wraps Pokemon Showdown's Gen 5 sim.
// Protocol: line-delimited JSON-RPC on stdin/stdout.
//   Request:  {"id": "<string>", "method": "<name>", "params": {...}}
//   Response: {"id": "<string>", "result": {...}}  or  {"id": "<string>", "error": "<msg>"}
//
// Methods: damage_range (working), execute_turn (stub), enumerate_turn (stub).
// Build PS first: `cd pokemon-showdown && node build`.

const readline = require('readline');
const path = require('path');

const PS_DIST = path.resolve(__dirname, '..', 'pokemon-showdown', 'dist', 'sim');
const { Battle, Dex, Teams } = require(PS_DIST);

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
    // Gen 5 Doubles starts in teampreview; default team order populates active slots.
    if (battle.requestState === 'teampreview') {
        battle.makeChoices('default', 'default');
    }
    return battle;
}


// Damage roll: PS applies floor(baseDamage * (100 - random(16)) / 100).
// roll=0 → max damage (100%); roll=15 → min damage (85%).
function forceDamageRoll(battle, roll) {
    battle.randomizer = (baseDamage) => Math.floor(baseDamage * (100 - roll) / 100);
}


// --- handlers ---

function handleDamageRange(params) {
    const { attacker, defender, move } = params;
    if (!attacker || !defender || !move) {
        throw new Error('damage_range requires attacker, defender, move');
    }

    // Pad each side with a filler mon so Gen 5 Doubles has two active slots.
    const filler = { species: 'Ditto', ability: 'Limber', moves: ['Transform'], nature: 'Serious', level: 1 };
    const battle = buildBattle([attacker, filler], [defender, filler]);

    // Advance to turn 1 so active slots are populated. Both sides default to picking
    // their first move on both slots; we never actually execute past setup because
    // getDamage is called directly below.
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


function handleExecuteTurn(params) {
    return { todo: 'execute_turn not implemented in skeleton' };
}


function handleEnumerateTurn(params) {
    return { todo: 'enumerate_turn not implemented in skeleton' };
}


const handlers = {
    damage_range: handleDamageRange,
    execute_turn: handleExecuteTurn,
    enumerate_turn: handleEnumerateTurn,
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
        process.stdout.write(JSON.stringify({ id, error: `${e.message}` }) + '\n');
    }
});

rl.on('close', () => { process.exit(0); });
