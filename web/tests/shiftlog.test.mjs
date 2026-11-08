// shiftlog.js across tabs: one log id per log, ?fresh not undone by an older tab, one copy
// of an automatic entry logged by two tabs, identical manual entries kept.
import assert from "node:assert/strict";
const SRC = new URL("../src", import.meta.url).href;
const mem = new Map();
globalThis.localStorage = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, String(v)), removeItem: (k) => mem.delete(k) };
globalThis.setInterval = () => 0;
let now = 1_000_000; Date.now = () => now;
const tab = async (search, n) => { globalThis.location = { search }; return import(`${SRC}/shiftlog.js?tab=${n}`); };
const texts = (m) => m.entries().map((e) => e.text);

const A = await tab("", "A");
A.start("01:47", "inc");
A.add("approve", "APPROVED rehearsal plan.");
now += 5 * 60000;
const B = await tab("?fresh", "B");
B.start("01:47", "inc");
B.add("open", "Console opened (demo).");
A.add("alarm", "Tank 3 high level.");
assert.deepEqual(texts(B), ["Console opened (demo)."], "an older tab never writes its log into a fresh one");
assert.ok(texts(A).includes("Tank 3 high level."), "the older tab keeps its own entries in memory");

// A reload (no ?fresh) within 30 min picks up the fresh log, not the rehearsal.
const B2 = await tab("", "B2");
B2.start("01:47", "inc");
assert.deepEqual(texts(B2), ["Console opened (demo)."]);

// Two tabs on one log: an automatic entry both log is kept once; both see each other's entries.
mem.clear();
const C = await tab("?fresh", "C"); C.start("01:47", "X");
const D = await tab("", "D"); D.start("01:47", "X");
C.add("alarm", "MISSED CHECK-IN: Rover.");
D.add("alarm", "MISSED CHECK-IN: Rover.");
assert.equal(texts(C).length, 1);
D.add("note", "Called the rover.");
assert.deepEqual(texts(C), ["MISSED CHECK-IN: Rover.", "Called the rover."]);

// Same tab, the same note twice in a minute: both kept.
mem.clear();
const E = await tab("?fresh", "E"); E.start("01:47", "Y");
E.add("note", "Called rover."); E.add("note", "Called rover.");
assert.equal(E.entries().length, 2);

// Older than 30 min: a new log.
now += 31 * 60000;
const F = await tab("", "F"); F.start("01:47", "Y");
assert.equal(F.entries().length, 0, "a log nobody had open for 30 min is a rehearsal");
// Logged before start() (a /ws alert during the model load): kept once, stamped on the shift clock.
mem.clear();
const G = await tab("?fresh", "G");
G.add("alert", "ALARM: Pump 3 tripped.");
G.add("alert", "ALARM: Pump 3 tripped.");
G.start("01:47", "Z");
assert.equal(G.entries().length, 0, "held until the console has written its first line");
G.add("open", "Console opened.");
G.flushEarly();
assert.deepEqual(G.entries().map((e) => `${e.at} ${e.text}`), ["1:47 AM Console opened.", "1:47 AM ALARM: Pump 3 tripped."]);
assert.ok(mem.has("freeboard.shiftlog.Z") && !mem.has("freeboard.shiftlog"), "stored under the incident's key");
console.log("shiftlog.js tabs: all checks pass");
process.exit(0);
