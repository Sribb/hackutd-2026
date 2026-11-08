// Talking to gaia (#171). Push to talk: hold the space bar (when no text field has focus) or
// click the mic. A calm orb shows the live transcript; releasing sends it. gaia's replies are
// read aloud with the browser's speechSynthesis, short (two sentences) and interruptible:
// speaking again, pressing space or an open palm stops it. A mute toggle sits next to the mic
// and is remembered in localStorage. Where the browser has no speech recognition (anything but
// Chrome and Edge, or offline), the mic is off and one line says to type instead.
// Nothing is recorded or stored here: the browser's recognizer turns speech into text.

const MUTE_KEY = "gaia.voice.muted";
const CALM = /samantha|google uk english female|google us english|serena|karen|moira|aria|jenny|zira/i;

const typing = (el) => !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);

// The words to say: two sentences at most, IDs read as names, no symbols read out loud.
export function spoken(text, max = 2) {
  const t = String(text || "").replace(/\s+/g, " ").trim();
  if (!t) return "";
  const parts = t.match(/[^.!?]+(?:[.!?]+(?=\s|$)|$)/g) || [t];
  return parts.slice(0, max).map((s) => s.trim()).join(" ")
    .replace(/\b([A-Z]+)_([A-Z0-9]+)(?:_([A-Z0-9]+))?\b/g, (_, a, b, c) => [a, b, c].filter(Boolean).join(" ").toLowerCase())
    .replace(/\$(\d[\d,]*)/g, "$1 dollars")
    .slice(0, 320);
}

function store(get, v) {
  try { if (get) return localStorage.getItem(MUTE_KEY) === "1"; localStorage.setItem(MUTE_KEY, v ? "1" : "0"); } catch { /* private window: in memory only */ }
  return false;
}

const SPEAKER = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 9.5h3l4-3.5v12l-4-3.5H4z"/><path class="ag-wave" d="M15 9a4 4 0 0 1 0 6M17.5 6.5a7.5 7.5 0 0 1 0 11"/><path class="ag-x" d="M15.5 9.5l5 5M20.5 9.5l-5 5"/></svg>`;

// mountVoice({ dock, input, mic, send(text) }) -> { speak(text), hush(), listening(), muted(), setMuted(v) }
export function mountVoice({ dock, input, mic, send }) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const synth = typeof window.speechSynthesis === "object" && window.speechSynthesis ? window.speechSynthesis : null;

  // The listening orb: in the dock, above the composer.
  const orb = document.createElement("div");
  orb.className = "ag-orb";
  orb.hidden = true;
  orb.setAttribute("role", "status");
  orb.innerHTML = `<i class="ag-ball" aria-hidden="true"></i><div class="ag-orb-words"><p class="ag-heard" aria-live="polite"></p><p class="ag-hint">listening · release to send</p></div>`;
  dock.insertBefore(orb, dock.firstChild);
  const heard = orb.querySelector(".ag-heard");
  const hint = orb.querySelector(".ag-hint");

  // The mute toggle, next to the mic.
  const mute = document.createElement("button");
  mute.type = "button";
  mute.className = "cs-mic ag-mute";
  mute.id = "agMute";
  mute.innerHTML = SPEAKER;
  mic.parentElement.insertBefore(mute, mic);
  let muted = store(true);
  const drawMute = () => {
    mute.classList.toggle("off", muted);
    mute.setAttribute("aria-pressed", String(!muted));
    mute.setAttribute("aria-label", muted ? "gaia is muted. Click to hear replies." : "gaia reads replies aloud. Click to mute.");
    mute.title = muted ? "replies are muted" : "gaia reads replies aloud";
  };
  drawMute();
  mute.onclick = () => { setMuted(!muted); };
  if (!synth) mute.hidden = true;

  // One line where voice can't work.
  const note = document.createElement("p");
  note.className = "ag-voice-note";
  note.textContent = "voice needs chrome or edge here. type instead.";
  note.hidden = !!SR;
  const form = dock.querySelector("form");
  if (form && form.nextSibling) dock.insertBefore(note, form.nextSibling); else dock.appendChild(note);

  mic.title = SR ? "hold space or click to talk to gaia" : "voice input isn't available in this browser. type instead.";
  mic.disabled = !SR;

  // ------------------------------------------------------------ listen ----
  let rec = null, text = "", sendOnEnd = false, hideT = 0;
  function show(words, hintText) {
    clearTimeout(hideT);
    orb.hidden = false;
    heard.textContent = words;
    hint.textContent = hintText;
    document.body.classList.add("ag-listening");
  }
  function hide(ms = 0) {
    clearTimeout(hideT);
    const go = () => { orb.hidden = true; document.body.classList.remove("ag-listening"); };
    if (ms) hideT = setTimeout(go, ms); else go();
  }

  function listen(hold) {
    hush();
    if (!SR) { show("voice needs chrome or edge here.", "type instead"); hide(2600); return; }
    if (rec) return;
    text = "";
    sendOnEnd = false;
    try {
      rec = new SR();
    } catch { rec = null; return; }
    rec.lang = "en-US";
    rec.interimResults = true;
    rec.continuous = !!hold;
    rec.maxAlternatives = 1;
    mic.classList.add("on");
    show("", hold ? "listening · release to send" : "listening · click again to send");
    rec.onresult = (e) => {
      let all = "", final = false;
      for (const r of e.results) { all += r[0].transcript; if (r.isFinal) final = true; }
      text = all;
      heard.textContent = all;
      input.value = all;
      if (final && !hold && all.trim()) { sendOnEnd = true; stopListening(); }
    };
    rec.onerror = (e) => {
      const why = e && e.error === "not-allowed" ? "the mic is blocked. type instead" : e && e.error === "no-speech" ? "didn't hear anything" : "didn't catch that. try again or type";
      sendOnEnd = false;
      show("", why);
      hide(2200);
    };
    rec.onend = () => {
      rec = null;
      mic.classList.remove("on");
      const said = text.trim();
      if (sendOnEnd && said) {
        show(said, "sent");
        hide(900);
        input.value = "";
        send(said);
      } else if (orb.querySelector(".ag-hint").textContent.startsWith("listening")) hide();
    };
    try { rec.start(); } catch { rec = null; mic.classList.remove("on"); hide(); }
  }
  function stopListening() { if (rec) { try { rec.stop(); } catch { /* already stopped */ } } }
  function release() { if (!rec) return; sendOnEnd = true; stopListening(); }

  mic.onclick = () => { if (rec) release(); else listen(false); };

  let spaceDown = false;
  addEventListener("keydown", (e) => {
    if (e.code !== "Space" || e.ctrlKey || e.metaKey || e.altKey || typing(e.target)) return;
    e.preventDefault();
    if (e.repeat || spaceDown) return;
    spaceDown = true;
    listen(true);
  });
  addEventListener("keyup", (e) => {
    if (e.code !== "Space" || !spaceDown) return;
    e.preventDefault();
    spaceDown = false;
    release();
  });
  addEventListener("blur", () => { if (spaceDown) { spaceDown = false; release(); } });

  // ------------------------------------------------------------- speak ----
  let voice = null;
  const pickVoice = () => {
    if (!synth || voice) return voice;
    const vs = (synth.getVoices && synth.getVoices()) || [];
    voice = vs.find((v) => CALM.test(v.name) && /^en/i.test(v.lang)) || vs.find((v) => /^en/i.test(v.lang)) || null;
    return voice;
  };
  if (synth && "onvoiceschanged" in synth) synth.onvoiceschanged = () => { voice = null; pickVoice(); };

  function speak(t) {
    if (!synth || muted || rec) return false;
    const words = spoken(t);
    if (!words) return false;
    try {
      synth.cancel();
      const u = new SpeechSynthesisUtterance(words);
      const v = pickVoice();
      if (v) u.voice = v;
      u.lang = (v && v.lang) || "en-US";
      u.rate = 1.02;
      u.pitch = 1;
      u.onstart = () => mute.classList.add("talking");
      u.onend = u.onerror = () => mute.classList.remove("talking");
      synth.speak(u);
      return true;
    } catch { return false; }
  }
  function hush() {
    mute.classList.remove("talking");
    try { if (synth && (synth.speaking || synth.pending)) synth.cancel(); } catch { /* nothing to stop */ }
  }
  function setMuted(v) {
    muted = !!v;
    store(false, muted);
    drawMute();
    if (muted) hush();
  }

  return { speak, hush, listening: () => !!rec, muted: () => muted, setMuted, supported: () => !!SR, stopListening: () => { sendOnEnd = false; stopListening(); hide(); } };
}
