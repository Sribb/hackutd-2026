// Plans one tank of a whole-system outage off the main thread: a 24-hour outage takes
// seconds per tank, so the page runs one worker per tank (up to the cores it has) and
// stays responsive meanwhile.
import { tankRow, doNothing } from "./plan.js";

self.onmessage = (e) => {
  const { id, tank, maxK } = e.data;
  try {
    self.postMessage({ id, row: tankRow(tank, maxK), nothing: doNothing(tank.input) });
  } catch (err) {
    self.postMessage({ id, error: String(err?.message || err) });
  }
};
