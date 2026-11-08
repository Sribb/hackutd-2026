// Freeboard live state in SpacetimeDB.
//
// The FastAPI server stays the source of every number (forecast, planner, costs).
// This module holds the live, shared state so every open console sees the same
// thing at once without polling: asset status, the clock and incident, what-if
// actions operators are trying, and the steps of the approved plan.
//
// v2 (#126) adds the live alerts, the proposed commands, and connector status, plus
// apply_batch, which writes everything that changed at one moment in one transaction.
// v2 only adds: every table, column, and reducer of v1 is unchanged, so a database
// published with v1 takes v2 without --delete-data.
//
// Names are snake_case on purpose, so they read the same under any case policy.
// Absent values use sentinels instead of options, which keeps HTTP calls plain:
//   level_pct = -1   not a tank, or no reading
//   ""               no position / reason / target

import { schema, table, t, SenderError } from 'spacetimedb/server';

// UNKNOWN: no fresh reading (#38's live layer). Never shown as safe.
const STATUSES = ['OK', 'WARNING', 'ALARM', 'OFFLINE', 'UNKNOWN'];
const ACTION_TYPES = ['SET_VALVE', 'SET_PUMP', 'CHOKE_WELL', 'SEND_TRUCK'];
const ID = /^[A-Z][A-Z0-9_]*$/;

const asset_state = table(
  { name: 'asset_state', public: true },
  {
    asset_id: t.string().primaryKey(),
    status: t.string(),
    level_pct: t.f64(),
    position: t.string(),
    reason: t.string(),
    updated_at: t.timestamp(),
  }
);

// One row, id 0.
const field_clock = table(
  { name: 'field_clock', public: true },
  {
    id: t.u32().primaryKey(),
    clock: t.string(),
    incident_root: t.string(),
    incident_summary: t.string(),
  }
);

// What-if actions operators are trying on the 3D field, shared live.
const operator_action = table(
  { name: 'operator_action', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    action_type: t.string(),
    asset_id: t.string(),
    value: t.string(),
    target_id: t.string(),
    at_min: t.u32(),
    operator: t.identity(),
    created_at: t.timestamp(),
  }
);

// Steps of the approved plan, which the 3D scene walks through in order.
const plan_step = table(
  { name: 'plan_step', public: true },
  {
    n: t.u32().primaryKey(),
    plan_id: t.string(),
    asset_id: t.string(),
    text: t.string(),
    done: t.bool(),
  }
);

// Identities allowed to write live state: the backend. Private, so consoles can't read it.
// init adds the identity that publishes the module. A database published before this
// table existed starts with it empty, and the first caller of claim_writer gets it, so
// the backend should call claim_writer right after a republish (spacetime_sync does).
const writer = table(
  { name: 'writer' },
  {
    identity: t.identity().primaryKey(),
  }
);

// ---------- v2 tables (#126) ----------
// Written only by apply_batch, from the backend's mirror (backend/app/spacetime_mirror.py).

// Alert severities from the live layer's detector (backend/app/detect.py).
const SEVERITIES = ['WARNING', 'ALARM'];
// A proposal's life (docs/CONTRACT.md, Actions). UNCERTAIN: the command left for the
// device but no acknowledgement came back, so a person checks the field before a retry.
const PROPOSAL_STATUSES = ['PENDING', 'SENDING', 'CONFIRMED', 'FAILED', 'REJECTED', 'UNCERTAIN'];
const CONNECTOR_STATUSES = ['connected', 'unavailable'];
// Record ids (alert_7, act_3), connector kinds (mqtt), and proposal sources (jarvis).
const LOWER_ID = /^[a-z][a-z0-9_]*$/;

// Every live alert, active or resolved (GET /api/alerts). A resolved alert stays, with
// active false. kind is an ID-style word (PUMP_TRIP), so a new detector kind needs no
// republish. raised_clock and resolved_clock are live clock times like "01:48".
const alert = table(
  { name: 'alert', public: true },
  {
    id: t.string().primaryKey(),
    asset_id: t.string(),
    kind: t.string(),
    severity: t.string(),
    message: t.string(),
    active: t.bool(),
    raised_clock: t.string(),
    resolved_clock: t.string(),
  }
);

// Proposed commands (GET /api/actions). This table only shows them: a command moves
// only when a person confirms it through the backend. value is text: OPEN, ON, 0.5, or "".
const proposal = table(
  { name: 'proposal', public: true },
  {
    id: t.string().primaryKey(),
    type: t.string(),
    asset_id: t.string(),
    value: t.string(),
    target_id: t.string(),
    status: t.string(),
    source: t.string(),
    reason: t.string(),
    decided_by: t.string(),
    result: t.string(),
  }
);

// Where live signals come from (GET /api/live/connectors). updated_clock is the live
// clock when the connector's status or detail last changed.
const connector_status = table(
  { name: 'connector_status', public: true },
  {
    id: t.string().primaryKey(),
    kind: t.string(),
    status: t.string(),
    detail: t.string(),
    updated_clock: t.string(),
  }
);

const spacetimedb = schema({
  asset_state, field_clock, operator_action, plan_step, writer,
  alert, proposal, connector_status,
});
export default spacetimedb;

function checkId(id: string, what: string) {
  if (!ID.test(id)) throw new SenderError(`${what} '${id}' is not a contract ID`);
}

// Consoles read everything; only the backend writes live state (D2).
function requireWriter(ctx: any) {
  for (const w of ctx.db.writer.iter()) {
    if (w.identity.isEqual(ctx.sender)) return;
  }
  throw new SenderError('only the Freeboard backend can write live state');
}

export const init = spacetimedb.init(ctx => {
  ctx.db.field_clock.insert({ id: 0, clock: '', incident_root: '', incident_summary: '' });
  ctx.db.writer.insert({ identity: ctx.sender });
});

// Takes the writer role only while nobody has it (a database published before D2).
export const claim_writer = spacetimedb.reducer(ctx => {
  for (const w of ctx.db.writer.iter()) {
    if (w.identity.isEqual(ctx.sender)) return;
    throw new SenderError('the writer role is already taken');
  }
  ctx.db.writer.insert({ identity: ctx.sender });
});

// A writer can let another identity write, e.g. a second backend.
export const add_writer = spacetimedb.reducer({ identity: t.identity() }, (ctx, { identity }) => {
  requireWriter(ctx);
  if (!ctx.db.writer.identity.find(identity)) ctx.db.writer.insert({ identity });
});

export const upsert_asset = spacetimedb.reducer(
  {
    asset_id: t.string(),
    status: t.string(),
    level_pct: t.f64(),
    position: t.string(),
    reason: t.string(),
  },
  (ctx, { asset_id, status, level_pct, position, reason }) => {
    requireWriter(ctx);
    checkId(asset_id, 'asset_id');
    if (!STATUSES.includes(status)) throw new SenderError(`unknown status '${status}'`);
    if (level_pct !== -1 && !(level_pct >= 0 && level_pct <= 100)) {
      throw new SenderError(`level_pct ${level_pct} must be 0-100, or -1 for none`);
    }
    const row = { asset_id, status, level_pct, position, reason, updated_at: ctx.timestamp };
    if (ctx.db.asset_state.asset_id.find(asset_id)) ctx.db.asset_state.asset_id.update(row);
    else ctx.db.asset_state.insert(row);
  }
);

export const set_clock = spacetimedb.reducer(
  { clock: t.string(), incident_root: t.string(), incident_summary: t.string() },
  (ctx, { clock, incident_root, incident_summary }) => {
    requireWriter(ctx);
    if (incident_root) checkId(incident_root, 'incident_root');
    const row = { id: 0, clock, incident_root, incident_summary };
    if (ctx.db.field_clock.id.find(0)) ctx.db.field_clock.id.update(row);
    else ctx.db.field_clock.insert(row);
  }
);

export const propose_action = spacetimedb.reducer(
  {
    action_type: t.string(),
    asset_id: t.string(),
    value: t.string(),
    target_id: t.string(),
    at_min: t.u32(),
  },
  (ctx, { action_type, asset_id, value, target_id, at_min }) => {
    if (!ACTION_TYPES.includes(action_type)) {
      throw new SenderError(`unknown action type '${action_type}'`);
    }
    checkId(asset_id, 'asset_id');
    if (target_id) checkId(target_id, 'target_id');
    ctx.db.operator_action.insert({
      id: 0n, // autoInc assigns the real id
      action_type,
      asset_id,
      value,
      target_id,
      at_min,
      operator: ctx.sender,
      created_at: ctx.timestamp,
    });
  }
);

// Anyone can remove their own what-if; only the backend removes someone else's.
export const remove_action = spacetimedb.reducer({ id: t.u64() }, (ctx, { id }) => {
  const row = ctx.db.operator_action.id.find(id);
  if (!row) return;
  if (!row.operator.isEqual(ctx.sender)) requireWriter(ctx);
  ctx.db.operator_action.id.delete(id);
});

export const clear_actions = spacetimedb.reducer(ctx => {
  requireWriter(ctx);
  for (const row of ctx.db.operator_action.iter()) ctx.db.operator_action.id.delete(row.id);
});

// Replaces the approved plan. steps_json is the `steps` array from
// POST /api/plans/{id}/approve: [{ "n": 1, "asset_id": "...", "text": "..." }].
export const set_plan = spacetimedb.reducer(
  { plan_id: t.string(), steps_json: t.string() },
  (ctx, { plan_id, steps_json }) => {
    requireWriter(ctx);
    let steps: Array<{ n: number; asset_id: string; text: string }>;
    try {
      steps = JSON.parse(steps_json);
    } catch {
      throw new SenderError('steps_json is not valid JSON');
    }
    if (!Array.isArray(steps)) throw new SenderError('steps_json must be an array');
    for (const row of ctx.db.plan_step.iter()) ctx.db.plan_step.n.delete(row.n);
    for (const s of steps) {
      checkId(s.asset_id, 'asset_id');
      ctx.db.plan_step.insert({ n: s.n, plan_id, asset_id: s.asset_id, text: s.text, done: false });
    }
  }
);

export const mark_step_done = spacetimedb.reducer(
  { n: t.u32(), done: t.bool() },
  (ctx, { n, done }) => {
    requireWriter(ctx);
    const step = ctx.db.plan_step.n.find(n);
    if (!step) throw new SenderError(`no plan step ${n}`);
    ctx.db.plan_step.n.update({ ...step, done });
  }
);

// ---------- apply_batch (v2, #126) ----------
//
// One call per mirror tick: everything that changed on the live field at one moment, in
// one transaction. A bad row refuses the whole batch and nothing is written, so a console
// never sees half a tick. The backend checks rows the same way before it sends them
// (backend/app/spacetime_mirror.py), so one odd reading can't stop the mirror.
//
// json is one object, every key optional:
//   clock               { clock, incident_root, incident_summary }   like set_clock
//   assets              [{ asset_id, status, level_pct, position, reason }]   like upsert_asset
//   alerts              [{ id, asset_id, kind, severity, message, active, raised_clock, resolved_clock }]
//   proposals           [{ id, type, asset_id, value, target_id, status, source, reason, decided_by, result }]
//   connectors          [{ id, kind, status, detail, updated_clock }]
//   removed_assets, removed_alerts, removed_proposals, removed_connectors   [id, ...]
//   full                true: this batch is everything the backend knows, so rows it
//                       doesn't list are deleted (a replaced field's assets, alerts from
//                       before a backend restart). It must carry every list, even empty.
// Missing text is "". Removals run first, then upserts.

type Row = { [key: string]: unknown };

const LISTS = ['assets', 'alerts', 'proposals', 'connectors'];
const REMOVALS = ['removed_assets', 'removed_alerts', 'removed_proposals', 'removed_connectors'];
const BATCH_KEYS = ['full', 'clock', ...LISTS, ...REMOVALS];

function isObject(v: unknown): v is Row {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function rowsOf(batch: Row, key: string): Row[] {
  const v = batch[key];
  if (v === undefined || v === null) return [];
  if (!Array.isArray(v)) throw new SenderError(`${key} must be a list`);
  for (const r of v) {
    if (!isObject(r)) throw new SenderError(`every item in ${key} must be an object`);
  }
  return v as Row[];
}

function idsOf(batch: Row, key: string, check: (id: string, what: string) => void): string[] {
  const v = batch[key];
  if (v === undefined || v === null) return [];
  if (!Array.isArray(v)) throw new SenderError(`${key} must be a list`);
  for (const id of v) {
    if (typeof id !== 'string') throw new SenderError(`every id in ${key} must be text`);
    check(id, key);
  }
  return v as string[];
}

// A field that must be there, as text.
function need(r: Row, key: string, what: string): string {
  const v = r[key];
  if (typeof v !== 'string') throw new SenderError(`${what} needs ${key} as text`);
  return v;
}

// A text field that may be missing ("").
function text(r: Row, key: string, what: string): string {
  const v = r[key];
  if (v === undefined || v === null) return '';
  if (typeof v !== 'string') throw new SenderError(`${what}: ${key} must be text`);
  return v;
}

function oneOf(v: string, allowed: string[], what: string) {
  if (!allowed.includes(v)) throw new SenderError(`unknown ${what} '${v}'`);
}

function checkLowerId(id: string, what: string) {
  if (!LOWER_ID.test(id)) throw new SenderError(`${what} '${id}' must be lowercase letters, digits, and _`);
}

// The same checks as set_clock.
function putClock(ctx: any, c: unknown) {
  if (!isObject(c)) throw new SenderError('clock must be an object');
  const incident_root = text(c, 'incident_root', 'clock');
  if (incident_root) checkId(incident_root, 'incident_root');
  const row = {
    id: 0,
    clock: text(c, 'clock', 'clock'),
    incident_root,
    incident_summary: text(c, 'incident_summary', 'clock'),
  };
  if (ctx.db.field_clock.id.find(0)) ctx.db.field_clock.id.update(row);
  else ctx.db.field_clock.insert(row);
}

// The same checks as upsert_asset.
function putAsset(ctx: any, r: Row): string {
  const asset_id = need(r, 'asset_id', 'an asset');
  checkId(asset_id, 'asset_id');
  const what = `asset ${asset_id}`;
  const status = need(r, 'status', what);
  if (!STATUSES.includes(status)) throw new SenderError(`unknown status '${status}'`);
  const level_pct = r.level_pct;
  if (typeof level_pct !== 'number' || (level_pct !== -1 && !(level_pct >= 0 && level_pct <= 100))) {
    throw new SenderError(`level_pct ${level_pct} must be 0-100, or -1 for none`);
  }
  const row = {
    asset_id,
    status,
    level_pct,
    position: text(r, 'position', what),
    reason: text(r, 'reason', what),
    updated_at: ctx.timestamp,
  };
  if (ctx.db.asset_state.asset_id.find(asset_id)) ctx.db.asset_state.asset_id.update(row);
  else ctx.db.asset_state.insert(row);
  return asset_id;
}

function putAlert(ctx: any, r: Row): string {
  const id = need(r, 'id', 'an alert');
  checkLowerId(id, 'alert id');
  const what = `alert ${id}`;
  const asset_id = need(r, 'asset_id', what);
  checkId(asset_id, 'asset_id');
  const kind = need(r, 'kind', what);
  checkId(kind, 'alert kind');
  const severity = need(r, 'severity', what);
  oneOf(severity, SEVERITIES, 'severity');
  const active = r.active;
  if (typeof active !== 'boolean') throw new SenderError(`${what} needs active as true or false`);
  const row = {
    id,
    asset_id,
    kind,
    severity,
    message: text(r, 'message', what),
    active,
    raised_clock: text(r, 'raised_clock', what),
    resolved_clock: text(r, 'resolved_clock', what),
  };
  if (ctx.db.alert.id.find(id)) ctx.db.alert.id.update(row);
  else ctx.db.alert.insert(row);
  return id;
}

function putProposal(ctx: any, r: Row): string {
  const id = need(r, 'id', 'a proposal');
  checkLowerId(id, 'proposal id');
  const what = `proposal ${id}`;
  const type = need(r, 'type', what);
  if (!ACTION_TYPES.includes(type)) throw new SenderError(`unknown action type '${type}'`);
  const asset_id = need(r, 'asset_id', what);
  checkId(asset_id, 'asset_id');
  const target_id = text(r, 'target_id', what);
  if (target_id) checkId(target_id, 'target_id');
  const status = need(r, 'status', what);
  oneOf(status, PROPOSAL_STATUSES, 'proposal status');
  const source = text(r, 'source', what);
  if (source) checkLowerId(source, 'proposal source');
  const row = {
    id,
    type,
    asset_id,
    value: text(r, 'value', what),
    target_id,
    status,
    source,
    reason: text(r, 'reason', what),
    decided_by: text(r, 'decided_by', what),
    result: text(r, 'result', what),
  };
  if (ctx.db.proposal.id.find(id)) ctx.db.proposal.id.update(row);
  else ctx.db.proposal.insert(row);
  return id;
}

function putConnector(ctx: any, r: Row): string {
  const id = need(r, 'id', 'a connector');
  checkId(id, 'connector id');
  const what = `connector ${id}`;
  const kind = need(r, 'kind', what);
  checkLowerId(kind, 'connector kind');
  const status = need(r, 'status', what);
  oneOf(status, CONNECTOR_STATUSES, 'connector status');
  const row = {
    id,
    kind,
    status,
    detail: text(r, 'detail', what),
    updated_clock: text(r, 'updated_clock', what),
  };
  if (ctx.db.connector_status.id.find(id)) ctx.db.connector_status.id.update(row);
  else ctx.db.connector_status.insert(row);
  return id;
}

export const apply_batch = spacetimedb.reducer({ json: t.string() }, (ctx, { json }) => {
  requireWriter(ctx);
  let batch: unknown;
  try {
    batch = JSON.parse(json);
  } catch {
    throw new SenderError('json is not valid JSON');
  }
  if (!isObject(batch)) throw new SenderError('json must be an object');
  for (const key of Object.keys(batch)) {
    if (!BATCH_KEYS.includes(key)) throw new SenderError(`unknown batch key '${key}'`);
  }
  const full = batch.full ?? false;
  if (typeof full !== 'boolean') throw new SenderError('full must be true or false');
  if (full) {
    for (const key of ['clock', ...LISTS]) {
      if (batch[key] === undefined || batch[key] === null) {
        throw new SenderError(`a full batch must carry ${key}, even if empty`);
      }
    }
  }

  for (const id of idsOf(batch, 'removed_assets', checkId)) ctx.db.asset_state.asset_id.delete(id);
  for (const id of idsOf(batch, 'removed_alerts', checkLowerId)) ctx.db.alert.id.delete(id);
  for (const id of idsOf(batch, 'removed_proposals', checkLowerId)) ctx.db.proposal.id.delete(id);
  for (const id of idsOf(batch, 'removed_connectors', checkId)) ctx.db.connector_status.id.delete(id);

  if (batch.clock !== undefined && batch.clock !== null) putClock(ctx, batch.clock);
  const assets = new Set(rowsOf(batch, 'assets').map(r => putAsset(ctx, r)));
  const alerts = new Set(rowsOf(batch, 'alerts').map(r => putAlert(ctx, r)));
  const proposals = new Set(rowsOf(batch, 'proposals').map(r => putProposal(ctx, r)));
  const connectors = new Set(rowsOf(batch, 'connectors').map(r => putConnector(ctx, r)));

  if (full) {
    // Collected first, then deleted, so no table is changed while it is being read.
    const gone = (keys: string[], keep: Set<string>) => keys.filter(k => !keep.has(k));
    for (const id of gone(Array.from(ctx.db.asset_state.iter(), r => r.asset_id), assets)) {
      ctx.db.asset_state.asset_id.delete(id);
    }
    for (const id of gone(Array.from(ctx.db.alert.iter(), r => r.id), alerts)) ctx.db.alert.id.delete(id);
    for (const id of gone(Array.from(ctx.db.proposal.iter(), r => r.id), proposals)) {
      ctx.db.proposal.id.delete(id);
    }
    for (const id of gone(Array.from(ctx.db.connector_status.iter(), r => r.id), connectors)) {
      ctx.db.connector_status.id.delete(id);
    }
  }
});
