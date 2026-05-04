// Build data/abilities.json: map of species name → list of legal abilities
// (Gen 5, base + mod layered). Uses PS's `Dex.mod('gen5')` which already
// applies `inherit: true` semantics — base pokedex.ts first, overridden by
// mods/gen5/pokedex.ts for any entry that redefines `abilities`.
//
// Run:  node tools/build_abilities.js

'use strict';

const path = require('path');
const fs = require('fs');

const PS_DIST = path.resolve(__dirname, '..', 'pokemon-showdown', 'dist', 'sim');
const { Dex } = require(PS_DIST);

const gen5 = Dex.mod('gen5');
const out = {};

for (const species of gen5.species.all()) {
    // Skip cosmetic formes / Gmax / other entries PS ships but gen5 never sees.
    // Keep anything whose gen is <= 5 (isNonstandard can be "Past"/"Future").
    if (species.gen && species.gen > 5) continue;
    // Abilities is an object { "0": "...", "1"?: "...", "H"?: "...", "S"?: "..." }.
    // Preserve PS key order (slot 0 is the default in-game ability).
    const abilities = species.abilities || {};
    out[species.name] = {
        num: species.num,
        abilities: { ...abilities },
    };
}

const outPath = path.resolve(__dirname, '..', 'data', 'abilities.json');
fs.writeFileSync(outPath, JSON.stringify(out, null, 2));
console.log(`wrote ${Object.keys(out).length} species → ${outPath}`);

// Sanity check: Chandelure must have Shadow Tag as its hidden ability in gen5
// (mod override); base pokedex.ts has Infiltrator.
const chandelure = out['Chandelure'];
if (!chandelure || chandelure.abilities.H !== 'Shadow Tag') {
    console.error('SANITY FAIL: expected Chandelure.H === "Shadow Tag"; got',
        chandelure && chandelure.abilities);
    process.exit(1);
}
console.log('sanity: Chandelure.H === "Shadow Tag" ✓');
