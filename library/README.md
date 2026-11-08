# library

Presets and the shared asset library, in SpacetimeDB on Maincloud. The database is `hackutd-freeboard-library`, with its dashboard at https://spacetimedb.com/hackutd-freeboard-library.

Anyone can read it. Only admins can write: the identity that published the module, plus anyone it adds with `add_admin`.

| Table | What it holds |
|---|---|
| `field_preset` | Whole fields as blueprint JSON. `avon_field_6_tanks` is the demo field, and the generator's samples are added when they land. |
| `equipment_preset` | Defaults a human can apply to fill a gap in an uploaded blueprint, like a 500 bbl tank or a 130 bbl vacuum truck. Every preset says where its numbers came from. |
| `library_asset` | One row per file: category, license, source, sha256, size, triangles, and whether the upload is complete. Only CC0 files and the team's own files are accepted. |
| `asset_chunk` | The file contents, as 256 KB pieces of base64. |

## Reading a file

**Never subscribe to all of `asset_chunk`.** It holds 37 MB of files, about 50 MB as base64. Query one asset's chunks by `asset_id`, join them in `seq` order, and check the sha256 from `library_asset`.

Python: `.venv/bin/python -m library.fetch SCENERY_rock_1 --out rock_1.glb`

Over HTTP, for the web console:
1. `POST https://maincloud.spacetimedb.com/v1/identity` returns a token for any reader.
2. Send `POST /v1/database/hackutd-freeboard-library/sql` with that bearer token and a body like `SELECT seq, data_b64 FROM asset_chunk WHERE asset_id = 'SCENERY_rock_1'`.

## Writing

```bash
SPACETIME_TOKEN=<an admin token> .venv/bin/python -m library.upload \
  --db hackutd-freeboard-library --assets blender/assets
```

It uploads the field presets from `scenario/field.json` and `blender/samples/`, the equipment presets from `library/presets/equipment.json`, every file in the asset manifest, and `blender/field.glb`. A file that is already current is skipped.

## Republishing

```bash
spacetime publish -s maincloud --yes=remote -p library/spacetime hackutd-freeboard-library
```

Never pass a bare `--yes`. It also skips the delete-data confirmation.
