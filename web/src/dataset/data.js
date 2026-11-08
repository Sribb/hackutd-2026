// Loads one sponsor dataset folder (network.json, ops.json, telemetry.csv) and answers
// "what did sensor X read at time T". It never reads answer_key.json: the dev server
// does not serve it and nothing here asks for it.
//
// Two sources, same shape:
//   api   the backend's dataset mode (/api/dataset/...): folders, files, and alerts.
//   files the dev server's /eog/ folder, for the map and trends when no backend has
//         dataset mode yet. It has no detection, so alerts() returns null and the page
//         says so instead of showing an empty all-clear.

export const STEP_S = 300; // the telemetry grid: every series, every 5 minutes

// Parses telemetry.csv (timestamp,ts_unix,asset_id,metric,value) into one column per
// series on a shared time axis. Missing cells stay NaN, never 0.
export function parseTelemetry(text) {
  let pos = text.indexOf("\n");
  if (pos < 0) throw new Error("telemetry.csv is empty");
  const header = text.slice(0, pos).trim().split(",");
  const col = (name) => header.indexOf(name);
  const iTs = col("ts_unix"), iAsset = col("asset_id"), iMetric = col("metric"), iValue = col("value");
  if (iTs < 0 || iAsset < 0 || iMetric < 0 || iValue < 0)
    throw new Error("telemetry.csv needs ts_unix, asset_id, metric and value columns");

  const tsCol = [], keyCol = [], valCol = [];
  const keyIds = new Map();
  const keys = [];
  let min = Infinity, max = -Infinity;
  pos += 1;
  while (pos < text.length) {
    let end = text.indexOf("\n", pos);
    if (end < 0) end = text.length;
    const line = text.charCodeAt(end - 1) === 13 ? text.slice(pos, end - 1) : text.slice(pos, end);
    pos = end + 1;
    if (!line) continue;
    const f = line.split(",");
    const ts = Number(f[iTs]);
    const v = Number(f[iValue]);
    if (!Number.isFinite(ts)) continue;
    const key = f[iAsset] + "|" + f[iMetric];
    let id = keyIds.get(key);
    if (id === undefined) { id = keys.length; keyIds.set(key, id); keys.push(key); }
    tsCol.push(ts); keyCol.push(id); valCol.push(Number.isFinite(v) ? v : NaN);
    if (ts < min) min = ts;
    if (ts > max) max = ts;
  }
  if (!tsCol.length) throw new Error("telemetry.csv has no readings");

  const n = Math.round((max - min) / STEP_S) + 1;
  const times = new Float64Array(n);
  for (let i = 0; i < n; i++) times[i] = min + i * STEP_S;
  const cols = keys.map(() => new Float64Array(n).fill(NaN));
  for (let r = 0; r < tsCol.length; r++) {
    const i = Math.round((tsCol[r] - min) / STEP_S);
    cols[keyCol[r]][i] = valCol[r];
  }
  const series = new Map(keys.map((k, j) => [k, cols[j]]));
  return makeStore(times, series);
}

// Index of the last grid step at or before t (or -1).
export function indexAt(times, t) {
  if (!times.length || t < times[0]) return -1;
  const i = Math.floor((t - times[0]) / STEP_S);
  return Math.min(i, times.length - 1);
}

export function makeStore(times, series) {
  const start = times[0], now = times[times.length - 1];
  return {
    times, series, start, now,
    has: (asset, metric) => series.has(asset + "|" + metric),
    metrics(asset) {
      const out = [];
      for (const k of series.keys()) if (k.startsWith(asset + "|")) out.push(k.slice(asset.length + 1));
      return out;
    },
    // The reading at the last step at or before t. A reading older than maxAgeS is
    // treated as missing (null), never carried forward as if it were current.
    latest(asset, metric, t = now, maxAgeS = 2 * STEP_S) {
      const col = series.get(asset + "|" + metric);
      if (!col) return null;
      let i = indexAt(times, t);
      while (i >= 0 && Number.isNaN(col[i])) {
        if (t - times[i] > maxAgeS) return null;
        i--;
      }
      if (i < 0 || t - times[i] > maxAgeS) return null;
      return col[i];
    },
    // Mean of the readings in (t - windowS, t]. Smooths sensor noise for rates; null if none.
    // positiveOnly skips zero readings (a well that was shut in for part of the window).
    mean(asset, metric, t = now, windowS = 3600, positiveOnly = false) {
      const col = series.get(asset + "|" + metric);
      if (!col) return null;
      const b = indexAt(times, t);
      let sum = 0, n = 0;
      for (let i = b; i >= 0 && times[i] > t - windowS; i--) {
        const v = col[i];
        if (Number.isNaN(v) || (positiveOnly && v <= 0)) continue;
        sum += v; n++;
      }
      return n ? sum / n : null;
    },
    // [[ts, value], ...] between from and to (inclusive), NaN cells skipped.
    range(asset, metric, from = start, to = now) {
      const col = series.get(asset + "|" + metric);
      if (!col) return [];
      const a = Math.max(0, indexAt(times, from) < 0 ? 0 : Math.ceil((from - start) / STEP_S));
      const b = indexAt(times, to);
      const out = [];
      for (let i = a; i <= b; i++) if (!Number.isNaN(col[i])) out.push([times[i], col[i]]);
      return out;
    },
  };
}

// Every asset in network.json with its kind and the facility/system it belongs to.
export function assetIndex(network) {
  const out = new Map();
  for (const s of network.systems || []) {
    out.set(s.disposal_site?.id, { kind: "disposal", system: s.id, facility: null, ref: s.disposal_site });
    for (const f of s.facilities || []) {
      out.set(f.id, { kind: "facility", system: s.id, facility: f.id, ref: f });
      for (const w of f.wells || []) out.set(w.id, { kind: "well", system: s.id, facility: f.id, ref: w });
      if (f.tank) out.set(f.tank.id, { kind: "tank", system: s.id, facility: f.id, ref: f.tank });
      if (f.pump) out.set(f.pump.id, { kind: "pump", system: s.id, facility: f.id, ref: f.pump });
      if (f.pipeline) out.set(f.pipeline.id, { kind: "pipeline", system: s.id, facility: f.id, ref: f.pipeline });
    }
  }
  out.delete(undefined);
  return out;
}

// Checks the parts of network.json and ops.json the page reads, and says what is wrong
// in plain words. An unseen network (test/) that breaks a rule shows the reason.
export function checkDataset(network, ops) {
  const problems = [];
  if (!Array.isArray(network?.systems) || !network.systems.length) problems.push("network.json has no systems.");
  const lim = network?.defaults?.tank_limits_pct;
  if (!Number.isFinite(lim?.warning) || !Number.isFinite(lim?.shutdown)) problems.push("network.json has no tank limits.");
  if (!Number.isFinite(ops?.prices?.oil_per_bbl) || !Number.isFinite(ops?.prices?.gas_per_mcf)) problems.push("ops.json has no oil or gas price.");
  if (!Number.isFinite(ops?.expected_repair_hours?.pump_failure)) problems.push("ops.json has no pump repair time.");
  for (const s of network?.systems || []) {
    if (!ops?.systems?.[s.id]) problems.push(`ops.json has no trucks for ${s.id}.`);
    for (const f of s.facilities || []) {
      if (!ops?.systems?.[s.id]?.facilities?.[f.id]) problems.push(`ops.json has no truck times for ${f.id}.`);
      if (!f.tank || !f.pump || !f.pipeline) problems.push(`${f.id} is missing a tank, pump or pipeline.`);
    }
  }
  return problems;
}

async function getJSON(url, init) {
  const r = await fetch(url, { cache: "no-store", ...init });
  if (!r.ok) {
    const e = new Error(`${url} answered ${r.status}`);
    e.status = r.status;
    throw e;
  }
  return r.json();
}

async function getText(url, onProgress) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) {
    const e = new Error(`${url} answered ${r.status}`);
    e.status = r.status;
    throw e;
  }
  const total = Number(r.headers.get("Content-Length")) || 0;
  if (!r.body || !onProgress) return r.text();
  const reader = r.body.getReader();
  const parts = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    parts.push(value);
    got += value.length;
    onProgress(got, total);
  }
  const all = new Uint8Array(got);
  let o = 0;
  for (const p of parts) { all.set(p, o); o += p.length; }
  return new TextDecoder().decode(all);
}

// Orders folders the way a judge wants them: test/ first, then the dress rehearsal,
// the snapshots in time order, and the full week last.
export function sortFolders(list) {
  const rank = (f) => (/^test(\/|$)/.test(f) ? 0 : /mock_test/.test(f) ? 1 : /snapshot/.test(f) ? 2 : /full_week/.test(f) ? 3 : 4);
  return [...list].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}

export function apiSource(apiBase, authFetch = fetch) {
  const u = (p) => `${apiBase}/api/dataset${p}`;
  return {
    kind: "api",
    async info() { return getJSON(u("")); },
    async folders() { return sortFolders((await getJSON(u(""))).folders || []); },
    async load(folder, onProgress) {
      const r = await authFetch(u("/load"), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ folder }),
      });
      if (!r.ok) throw new Error(`The backend could not load ${folder} (${r.status}).`);
      const [network, ops, text] = await Promise.all([
        getJSON(u("/file/network.json")), getJSON(u("/file/ops.json")), getText(u("/file/telemetry.csv"), onProgress),
      ]);
      return { network, ops, store: parseTelemetry(text) };
    },
    async alerts(at) { return getJSON(u(`/alerts${at ? `?at=${Math.round(at)}` : ""}`)); },
    eventsUrl: (at) => u(`/events.json${at ? `?at=${Math.round(at)}` : ""}`),
  };
}

export function fileSource(base = "/eog") {
  return {
    kind: "files",
    async info() { return getJSON(`${base}/index.json`); },
    async folders() { return sortFolders((await getJSON(`${base}/index.json`)).folders || []); },
    async load(folder, onProgress) {
      const at = `${base}/${folder.split("/").map(encodeURIComponent).join("/")}`;
      const [network, ops, text] = await Promise.all([
        getJSON(`${at}/network.json`), getJSON(`${at}/ops.json`), getText(`${at}/telemetry.csv`, onProgress),
      ]);
      return { network, ops, store: parseTelemetry(text) };
    },
    async alerts() { return null; },
    eventsUrl: () => null,
  };
}
