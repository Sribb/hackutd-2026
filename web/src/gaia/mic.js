// The composer's mic: the browser's own speech recognition fills the input, and a final
// result sends it. Where the browser has none (or it is offline), the button says so and
// does nothing else. Nothing is recorded or stored here.

export function mountMic(btn, input, send) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    btn.disabled = true;
    btn.title = "Voice input isn't available in this browser. Type instead.";
    return { listening: () => false };
  }
  let rec = null;
  const stop = () => { if (rec) { try { rec.stop(); } catch { /* already stopped */ } } };
  btn.onclick = () => {
    if (rec) { stop(); return; }
    rec = new SR();
    rec.lang = "en-US";
    rec.interimResults = true;
    rec.maxAlternatives = 1;
    btn.classList.add("on");
    rec.onresult = (e) => {
      let text = "", final = false;
      for (const r of e.results) { text += r[0].transcript; if (r.isFinal) final = true; }
      input.value = text;
      if (final && text.trim()) { stop(); send(); }
    };
    rec.onerror = (e) => { input.placeholder = e.error === "not-allowed" ? "the mic is blocked. type instead" : "didn't catch that. try again or type"; };
    rec.onend = () => { rec = null; btn.classList.remove("on"); };
    try { rec.start(); } catch { rec = null; btn.classList.remove("on"); }
  };
  return { listening: () => !!rec };
}
