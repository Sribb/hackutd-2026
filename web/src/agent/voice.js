// Talking to gaia (#171): speech is the main way to drive the console, typing the fallback.
// Push to talk: hold the space bar (when no text field has focus), press and hold any mic
// (the big one in the empty chat, the composer's, the field toolbar's), or click one to start
// and click again to send. A calm orb shows the live transcript; releasing sends it.
// gaia answers on screen only: nothing is read aloud (Sriyan's call, board 16:07 UTC Nov 8).
// Where the browser has no speech recognition (anything but Chrome and Edge, or offline), the
// mics are off and one line says to type instead.
// Nothing is recorded or stored here: the browser's recognizer turns speech into text.

const typing = (el) => !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);

// mountVoice({ dock, input, mic, mics, send(text) })
//   -> { listening(), supported(), stopListening(), addMic(button) }
export function mountVoice({ dock, input, mic, mics = [], send }) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

  // The listening orb: in the dock, above the composer.
  const orb = document.createElement("div");
  orb.className = "ag-orb";
  orb.hidden = true;
  orb.setAttribute("role", "status");
  orb.innerHTML = `<i class="ag-ball" aria-hidden="true"></i><div class="ag-orb-words"><p class="ag-heard" aria-live="polite"></p><p class="ag-hint">listening · release to send</p></div>`;
  dock.insertBefore(orb, dock.firstChild);
  const heard = orb.querySelector(".ag-heard");
  const hint = orb.querySelector(".ag-hint");

  // One line where voice can't work.
  const note = document.createElement("p");
  note.className = "ag-voice-note";
  note.textContent = "voice needs chrome or edge here. type instead.";
  note.hidden = !!SR;
  const form = dock.querySelector("form");
  if (form && form.nextSibling) dock.insertBefore(note, form.nextSibling); else dock.appendChild(note);


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
    setOn(true);
    show("", hold ? "listening · release to send" : "listening · click again to send");
    dispatchEvent(new Event("gaia:listening")); // the console opens the chat so the transcript shows
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
      setOn(false);
      const said = text.trim();
      if (sendOnEnd && said) {
        show(said, "sent");
        hide(900);
        input.value = "";
        send(said);
      } else if (orb.querySelector(".ag-hint").textContent.startsWith("listening")) hide();
    };
    try { rec.start(); } catch { rec = null; setOn(false); hide(); }
  }
  function stopListening() { if (rec) { try { rec.stop(); } catch { /* already stopped */ } } }
  function release() { if (!rec) return; sendOnEnd = true; stopListening(); }

  // Every mic: a click starts and a second click sends; press and hold talks until release.
  const all = [];
  function setOn(v) { for (const b of all) { b.classList.toggle("on", v); b.setAttribute("aria-pressed", String(v)); } document.body.classList.toggle("ag-talking", v); }
  function addMic(b) {
    if (!b || all.includes(b)) return;
    all.push(b);
    b.title = SR ? "hold to talk to gaia, or click to start and click again to send (or hold space)" : "voice input isn't available in this browser. type instead.";
    b.disabled = !SR;
    b.setAttribute("aria-pressed", "false");
    let held = 0, holding = false;
    b.addEventListener("pointerdown", (e) => {
      if (!SR || e.button !== 0 || rec) return;
      held = setTimeout(() => { holding = true; listen(true); }, 260);
    });
    const up = () => { clearTimeout(held); if (holding) { holding = false; b._skip = true; release(); } };
    b.addEventListener("pointerup", up);
    b.addEventListener("pointerleave", up);
    b.addEventListener("click", () => {
      if (b._skip) { b._skip = false; return; }
      if (rec) release(); else listen(false);
    });
  }
  for (const b of [mic, ...mics]) addMic(b);
  addEventListener("gaia:talk", () => { if (rec) release(); else listen(false); });

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

  return { listening: () => !!rec, supported: () => !!SR, addMic, stopListening: () => { sendOnEnd = false; stopListening(); hide(); } };
}
