// The judge's phone number for gaia's alerts (start page, #147). Pure functions plus one
// sessionStorage key. Nothing here sends the number anywhere: the SMS and voice provider is
// not chosen yet, so the number only lives in this tab until it closes.
//
//   parseUsPhone("(214) 555-0134")  -> "+12145550134"
//   parseUsPhone("123")             -> null
//
// Accepted: 10 digits, or 11 starting with 1, written with spaces, dashes, dots, parens and
// an optional leading "+1". The area code and the exchange cannot start with 0 or 1 (NANP).

export const PHONE_KEY = "gaia.judge.phone";

export function parseUsPhone(raw) {
  const s = String(raw ?? "").trim();
  if (!s || s.length > 24) return null;
  if (!/^\+?[\d\s().-]+$/.test(s)) return null;
  let d = s.replace(/\D/g, "");
  if (s.startsWith("+") && !d.startsWith("1")) return null;
  if (d.length === 11 && d.startsWith("1")) d = d.slice(1);
  if (d.length !== 10) return null;
  if (!/^[2-9]\d{2}[2-9]\d{6}$/.test(d)) return null;
  return `+1${d}`;
}

// "+12145550134" -> "(214) 555-0134", for showing the number back to the judge.
export function prettyUsPhone(e164) {
  const d = String(e164 || "").replace(/^\+1/, "");
  return /^\d{10}$/.test(d) ? `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}` : "";
}

export function savePhone(e164, storage = globalThis.sessionStorage) {
  try { if (e164) storage.setItem(PHONE_KEY, e164); else storage.removeItem(PHONE_KEY); return true; }
  catch { return false; } // private mode or storage off: the flow still goes on
}

export function readPhone(storage = globalThis.sessionStorage) {
  try { return storage.getItem(PHONE_KEY) || ""; } catch { return ""; }
}
