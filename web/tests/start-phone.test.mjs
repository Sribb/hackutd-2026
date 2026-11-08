// The judge's phone number on the start page (web/src/start/phone.js): what is accepted,
// what is refused, and that it only ever lands in the one sessionStorage key.
import assert from "node:assert/strict";
import { PHONE_KEY, parseUsPhone, prettyUsPhone, readPhone, savePhone } from "../src/start/phone.js";

const ok = {
  "(214) 555-0134": "+12145550134",
  "214-555-0134": "+12145550134",
  "214 555 0134": "+12145550134",
  "2145550134": "+12145550134",
  "+1 (214) 555-0134": "+12145550134",
  "+12145550134": "+12145550134",
  "1 214 555 0134": "+12145550134",
  "  214.555.0134 ": "+12145550134",
};
for (const [raw, want] of Object.entries(ok)) assert.equal(parseUsPhone(raw), want, raw);

for (const raw of ["", "123", "555-0134", "(014) 555-0134", "(214) 155-0134", "+44 20 7946 0958", "+2145550134",
  "214555013", "21455501345", "214-555-0134 ext 2", "call me", null, undefined, "1".repeat(40)]) {
  assert.equal(parseUsPhone(raw), null, String(raw));
}

assert.equal(prettyUsPhone("+12145550134"), "(214) 555-0134");
assert.equal(prettyUsPhone("nope"), "");

const mem = new Map();
const storage = { setItem: (k, v) => mem.set(k, String(v)), getItem: (k) => mem.get(k) ?? null, removeItem: (k) => mem.delete(k) };
assert.equal(savePhone("+12145550134", storage), true);
assert.deepEqual([...mem.keys()], [PHONE_KEY]);
assert.equal(PHONE_KEY, "gaia.judge.phone");
assert.equal(readPhone(storage), "+12145550134");
savePhone("", storage);
assert.equal(readPhone(storage), "");
const broken = { setItem() { throw new Error("off"); }, getItem() { throw new Error("off"); }, removeItem() { throw new Error("off"); } };
assert.equal(savePhone("+12145550134", broken), false);
assert.equal(readPhone(broken), "");

console.log("start-phone: ok");
