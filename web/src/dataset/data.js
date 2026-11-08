// Loads one sponsor dataset folder (network.json, ops.json, telemetry.csv) and answers
// "what did sensor X read at time T". It never reads answer_key.json: the dev server
// does not serve it and nothing here asks for it.
//
// The files come from the dev server's /eog/ folder (vite.config.js), the same folder the
// backend's dataset mode reads, or from the backend itself when there is no dev server.
// Detection and plans are the backend's (eog.js).

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

  // The grid is the 5-minute clock (ts_unix multiples of 300), whatever the first row is.
  const t0 = Math.floor(min / STEP_S) * STEP_S;
  const n = Math.floor((max - t0) / STEP_S) + 1;
  if (n > 60 * 24 * 12 * 2) // two months of 5-minute steps: a bad timestamp, not a week of data
    throw new Error(`telemetry.csv runs from ${new Date(min * 1000).toISOString()} to ${new Date(max * 1000).toISOString()}: check for a bad ts_unix`);
  const times = new Float64Array(n);
  for (let i = 0; i < n; i++) times[i] = t0 + i * STEP_S;
  const cols = keys.map(() => new Float64Array(n).fill(NaN));
  for (let r = 0; r < tsCol.length; r++) {
    // A row between grid steps belongs to the step at or before it, so it is never shown
    // earlier than it was read. A later row for the same cell wins.
    const i = Math.floor((tsCol[r] - t0) / STEP_S);
    cols[keyCol[r]][i] = valCol[r];
  }
  const series = new Map(keys.map((k, j) => [k, cols[j]]));
  return makeStore(times, series, max); // SPEC: "now" is the last ts_unix in the file
}

// Index of the last grid step at or before t (or -1).
export function indexAt(times, t) {
  if (!times.length || t < times[0]) return -1;
  const i = Math.floor((t - times[0]) / STEP_S);
  return Math.min(i, times.length - 1);
}

export function makeStore(times, series, lastTs) {
  const start = times[0], now = lastTs ?? times[times.length - 1];
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
    // Least-squares line through the last n readings at or before t, read at t. For a
    // level that is rising steadily (pump down) this beats one noisy sample. null if fewer
    // than 3 readings.
    fitAt(asset, metric, t = now, n = 6) {
      const col = series.get(asset + "|" + metric);
      if (!col) return null;
      const xs = [], ys = [];
      for (let i = indexAt(times, t); i >= 0 && xs.length < n; i--) if (!Number.isNaN(col[i])) { xs.push(times[i] - t); ys.push(col[i]); }
      if (xs.length < 3) return null;
      const mx = xs.reduce((a, b) => a + b, 0) / xs.length, my = ys.reduce((a, b) => a + b, 0) / ys.length;
      let sxy = 0, sxx = 0;
      for (let i = 0; i < xs.length; i++) { sxy += (xs[i] - mx) * (ys[i] - my); sxx += (xs[i] - mx) ** 2; }
      return sxx ? my - (sxy / sxx) * mx : my;
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

// The same three files from the backend (/api/dataset/file/{name}?folder=, #152), for a build
// with no dev server (dist/ has no /eog/ folder). answer_key.json is never served there either.
export function backendFileSource(apiBase = "") {
  const u = (p) => `${apiBase}/api/dataset${p}`;
  return {
    kind: "backend",
    async info() { return getJSON(u("")); },
    async folders() { return sortFolders((await getJSON(u(""))).folders || []); },
    async load(folder, onProgress) {
      const f = (name) => u(`/file/${name}?folder=${encodeURIComponent(folder)}`);
      const [network, ops, text] = await Promise.all([getJSON(f("network.json")), getJSON(f("ops.json")), getText(f("telemetry.csv"), onProgress)]);
      return { network, ops, store: parseTelemetry(text) };
    },
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
  };
}
