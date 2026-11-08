// Writes a copy of the team scenario with no scheduled faults, for the console test that needs
// a fresh root-cause alarm to arrive after the page has loaded (spec §7 Track B check 2).
// Usage: node tests/support/clean-scenario.mjs <backend repo dir> <out file>
import fs from "node:fs";
import path from "node:path";

const [dir = "..", out = path.join(process.env.TMPDIR || "/tmp", "freeboard-clean-scenario.json")] = process.argv.slice(2);
const src = path.resolve(dir, "scenario/field.json");
const sc = JSON.parse(fs.readFileSync(src, "utf8"));
sc.faults = [];
delete sc.failures;
sc.name = `${sc.name || "field"} (no faults, console tests)`;
fs.writeFileSync(out, JSON.stringify(sc, null, 1));
console.log(`clean scenario: ${out}`);
