// Freeboard live state in SpacetimeDB.
//
// The FastAPI server stays the source of every number (forecast, planner, costs).
// This module holds the live, shared state so every open console sees the same
// thing at once without polling: asset status, the clock and incident, what-if
// actions operators are trying, and the steps of the approved plan.
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

const spacetimedb = schema({ asset_state, field_clock, operator_action, plan_step, writer });
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
