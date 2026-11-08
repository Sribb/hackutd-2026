// Freeboard library in SpacetimeDB: field presets, equipment presets, and the asset library.
//
// Anyone can read. Only admins can write. The identity that publishes the module
// becomes the first admin in init.
//
// Files are stored in chunks of base64 text, because one reducer call has to stay
// small. A client rebuilds a file by reading its chunks in seq order and checking the
// sha256 from library_asset. A file only counts once finish_asset marks it complete.

import { schema, table, t, SenderError } from 'spacetimedb/server';

const SLUG = /^[A-Za-z0-9][A-Za-z0-9_./-]{0,159}$/;
const KINDS = ['tank', 'pump', 'discharge_valve', 'crossover_valve', 'well', 'disposal_well', 'truck', 'yard', 'costs', 'lead_min'];

const admin = table({ name: 'admin' }, { identity_hex: t.string().primaryKey() });

const field_preset = table(
  { name: 'field_preset', public: true },
  {
    preset_id: t.string().primaryKey(),
    name: t.string(),
    description: t.string(),
    tank_count: t.u32(),
    well_count: t.u32(),
    blueprint_json: t.string(),
    updated_at: t.timestamp(),
  }
);

const equipment_preset = table(
  { name: 'equipment_preset', public: true },
  {
    preset_id: t.string().primaryKey(),
    kind: t.string(),
    name: t.string(),
    values_json: t.string(),
    source: t.string(),
    updated_at: t.timestamp(),
  }
);

const library_asset = table(
  { name: 'library_asset', public: true },
  {
    asset_id: t.string().primaryKey(),
    library: t.string(),
    category: t.string(),
    file_name: t.string(),
    media_type: t.string(),
    bytes: t.u64(),
    sha256: t.string(),
    chunk_count: t.u32(),
    license: t.string(),
    source_url: t.string(),
    author: t.string(),
    triangles: t.u32(),
    notes: t.string(),
    complete: t.bool(),
    updated_at: t.timestamp(),
  }
);

const asset_chunk = table(
  { name: 'asset_chunk', public: true },
  {
    chunk_id: t.string().primaryKey(),
    asset_id: t.string(),
    seq: t.u32(),
    data_b64: t.string(),
  }
);

const spacetimedb = schema({ admin, field_preset, equipment_preset, library_asset, asset_chunk });
export default spacetimedb;

type Ctx = { sender: { toHexString(): string }; db: any };

function requireAdmin(ctx: Ctx) {
  if (!ctx.db.admin.identity_hex.find(ctx.sender.toHexString())) {
    throw new SenderError('only a library admin can change the library');
  }
}

function checkSlug(id: string, what: string) {
  if (!SLUG.test(id)) throw new SenderError(`${what} '${id}' must be letters, digits, _ . / or -`);
}

function parseJson(text: string, what: string): any {
  try {
    return JSON.parse(text);
  } catch {
    throw new SenderError(`${what} is not valid JSON`);
  }
}

function dropChunks(ctx: Ctx, asset_id: string) {
  for (const row of ctx.db.asset_chunk.iter()) {
    if (row.asset_id === asset_id) ctx.db.asset_chunk.chunk_id.delete(row.chunk_id);
  }
}

export const init = spacetimedb.init(ctx => {
  ctx.db.admin.insert({ identity_hex: ctx.sender.toHexString() });
});

export const add_admin = spacetimedb.reducer({ identity_hex: t.string() }, (ctx, { identity_hex }) => {
  requireAdmin(ctx);
  if (!/^[0-9a-f]{64}$/.test(identity_hex)) throw new SenderError('identity_hex must be 64 hex characters');
  if (!ctx.db.admin.identity_hex.find(identity_hex)) ctx.db.admin.insert({ identity_hex });
});

export const upsert_field_preset = spacetimedb.reducer(
  { preset_id: t.string(), name: t.string(), description: t.string(), blueprint_json: t.string() },
  (ctx, { preset_id, name, description, blueprint_json }) => {
    requireAdmin(ctx);
    checkSlug(preset_id, 'preset_id');
    const bp = parseJson(blueprint_json, 'blueprint_json');
    if (!Array.isArray(bp.tanks) || bp.tanks.length === 0) throw new SenderError('a field preset needs at least one tank');
    const wells = Array.isArray(bp.wells) ? bp.wells.length : 0;
    const row = { preset_id, name, description, tank_count: bp.tanks.length, well_count: wells, blueprint_json, updated_at: ctx.timestamp };
    if (ctx.db.field_preset.preset_id.find(preset_id)) ctx.db.field_preset.preset_id.update(row);
    else ctx.db.field_preset.insert(row);
  }
);

export const upsert_equipment_preset = spacetimedb.reducer(
  { preset_id: t.string(), kind: t.string(), name: t.string(), values_json: t.string(), source: t.string() },
  (ctx, { preset_id, kind, name, values_json, source }) => {
    requireAdmin(ctx);
    checkSlug(preset_id, 'preset_id');
    if (!KINDS.includes(kind)) throw new SenderError(`unknown kind '${kind}'`);
    const values = parseJson(values_json, 'values_json');
    if (values === null || typeof values !== 'object' || Array.isArray(values)) throw new SenderError('values_json must be an object');
    if (!source) throw new SenderError('every preset needs a source, so nobody mistakes a placeholder for a real number');
    const row = { preset_id, kind, name, values_json, source, updated_at: ctx.timestamp };
    if (ctx.db.equipment_preset.preset_id.find(preset_id)) ctx.db.equipment_preset.preset_id.update(row);
    else ctx.db.equipment_preset.insert(row);
  }
);

export const delete_preset = spacetimedb.reducer({ preset_id: t.string() }, (ctx, { preset_id }) => {
  requireAdmin(ctx);
  ctx.db.field_preset.preset_id.delete(preset_id);
  ctx.db.equipment_preset.preset_id.delete(preset_id);
});

// Starts (or restarts) an upload. Old chunks for this asset are dropped.
export const begin_asset = spacetimedb.reducer(
  {
    asset_id: t.string(), library: t.string(), category: t.string(), file_name: t.string(), media_type: t.string(),
    bytes: t.u64(), sha256: t.string(), chunk_count: t.u32(), license: t.string(), source_url: t.string(),
    author: t.string(), triangles: t.u32(), notes: t.string(),
  },
  (ctx, a) => {
    requireAdmin(ctx);
    checkSlug(a.asset_id, 'asset_id');
    if (!/^[0-9a-f]{64}$/.test(a.sha256)) throw new SenderError('sha256 must be 64 hex characters');
    if (a.library !== 'freeboard' && !a.license.startsWith('CC0')) {
      throw new SenderError(`only CC0 assets go in the shared library, got '${a.license}'`);
    }
    if (a.chunk_count === 0) throw new SenderError('chunk_count must be at least 1');
    dropChunks(ctx, a.asset_id);
    const row = { ...a, complete: false, updated_at: ctx.timestamp };
    if (ctx.db.library_asset.asset_id.find(a.asset_id)) ctx.db.library_asset.asset_id.update(row);
    else ctx.db.library_asset.insert(row);
  }
);

export const put_chunk = spacetimedb.reducer(
  { asset_id: t.string(), seq: t.u32(), data_b64: t.string() },
  (ctx, { asset_id, seq, data_b64 }) => {
    requireAdmin(ctx);
    const asset = ctx.db.library_asset.asset_id.find(asset_id);
    if (!asset) throw new SenderError(`no asset '${asset_id}', call begin_asset first`);
    if (seq >= asset.chunk_count) throw new SenderError(`seq ${seq} is past chunk_count ${asset.chunk_count}`);
    const chunk_id = `${asset_id}#${seq}`;
    const row = { chunk_id, asset_id, seq, data_b64 };
    if (ctx.db.asset_chunk.chunk_id.find(chunk_id)) ctx.db.asset_chunk.chunk_id.update(row);
    else ctx.db.asset_chunk.insert(row);
  }
);

export const finish_asset = spacetimedb.reducer({ asset_id: t.string() }, (ctx, { asset_id }) => {
  requireAdmin(ctx);
  const asset = ctx.db.library_asset.asset_id.find(asset_id);
  if (!asset) throw new SenderError(`no asset '${asset_id}'`);
  for (let seq = 0; seq < asset.chunk_count; seq++) {
    if (!ctx.db.asset_chunk.chunk_id.find(`${asset_id}#${seq}`)) throw new SenderError(`chunk ${seq} of '${asset_id}' is missing`);
  }
  ctx.db.library_asset.asset_id.update({ ...asset, complete: true, updated_at: ctx.timestamp });
});

export const delete_asset = spacetimedb.reducer({ asset_id: t.string() }, (ctx, { asset_id }) => {
  requireAdmin(ctx);
  dropChunks(ctx, asset_id);
  ctx.db.library_asset.asset_id.delete(asset_id);
});
