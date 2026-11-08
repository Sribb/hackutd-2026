// gaia as an agent (#171): it works on the screen in steps, talks, and listens.
//   steps.js     plays a reply's UI directives one by one under a ticking step list
//   voice.js     push to talk (hold space or click the mic) and spoken replies, with mute
//   playbook.js  "walk me through it" with no Claude: the same steps from the console's data
// console/index.js mounts this once and calls play(), spoke(), before() and stop(); the open
// palm (hands.js, agentic mode) calls stop() through the "gaia:palm" event.

import { makeSteps } from "./steps.js";
import { mountVoice } from "./voice.js";
import { walkthrough, WALK } from "./playbook.js";
import { normalize } from "../console/intents.js";
import "./agent.css";

const ABOUT = /\b(this|it|its|it's|that|this one|that one)\b/;
const ASKS = /\b(wrong|status|how'?s|how is|doing|explain|tell me|about|happening|going on|okay)\b/;

// H: { side, stream, apply(d), nice, hasId, selected, brief, alerts, unknown, tankOf,
//      say(text, extra), ask(text), closePanels(), chart(text) -> Promise, BASE, getJson(path) }
export function mountAgent(H) {
  // A "chart" step is a chart card (#159, drawn from the data, no AI); the rest are directives.
  const apply = (d) => (d.action === "chart" ? (H.chart ? H.chart(d.text) : "") : H.apply(d));
  const steps = makeSteps({ stream: H.stream, apply, nice: H.nice });
  const voice = mountVoice({
    dock: H.side.el.querySelector(".cs-dock"), input: H.side.input, mic: H.side.mic,
    send: (text) => H.ask(text),
  });

  // Claude is there to take a walk-through only when the backend says so; else the playbook.
  let llm = false;
  async function checkLlm() {
    if (!H.BASE) { llm = false; return; }
    try { const j = await H.getJson("/api/live/connectors"); llm = !!(j && j.jarvis && j.jarvis.llm_available); } catch { llm = false; }
  }
  checkLlm();
  setInterval(checkLlm, 60000);

  // Before the console routes a question: true when the agent answered it here.
  function before(text) {
    const t = normalize(text).trim();
    if (WALK.test(t) && !llm) {
      const w = walkthrough({ brief: H.brief, alerts: H.alerts, unknown: H.unknown, nice: H.nice, hasId: H.hasId, tankOf: H.tankOf, selected: H.selected });
      H.stream.showTyping("J");
      setTimeout(async () => {
        H.stream.hideTyping("J");
        await steps.play(w.steps);
        H.say(w.reply, { ids: w.ids, tag: "playbook" });
      }, 200);
      return true;
    }
    // "What's wrong with this?": fly to the selection now; the words come from the backend,
    // which gets the selection with the question.
    const sel = H.selected();
    if (sel && ABOUT.test(t) && ASKS.test(t)) H.apply({ action: "focus", asset_id: sel });
    return false;
  }

  function stop() {
    steps.cancel();
    voice.hush();
    voice.stopListening();
    H.closePanels();
  }
  addEventListener("gaia:palm", stop);

  return {
    play: (list) => steps.play(list),
    spoke: (text) => voice.speak(text),
    hush: () => voice.hush(),
    before,
    stop,
    state: () => ({ llm, busy: steps.busy(), listening: voice.listening(), muted: voice.muted(), voice: voice.supported() }),
  };
}
