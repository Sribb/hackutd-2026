"""Download one asset from the library and check it against its sha256.

    .venv/bin/python -m library.fetch SCENERY_rock_1 --db hackutd-freeboard-library --out rock_1.glb

Reading needs a token from any identity, not an admin. A fresh anonymous identity works:
POST https://maincloud.spacetimedb.com/v1/identity returns one. The token is used and
not saved.
"""
import argparse
import base64
import hashlib
import json
import sys
import urllib.request
from pathlib import Path


def _post(url, body, token=None, content_type="text/plain"):
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.loads(res.read() or b"null")


def fetch(asset_id, db, url="https://maincloud.spacetimedb.com"):
    url = url.rstrip("/")
    token = _post(f"{url}/v1/identity", b"")["token"]
    quoted = asset_id.replace("'", "''")

    def sql(query):
        out = _post(f"{url}/v1/database/{db}/sql", query.encode(), token)
        return [row for result in out for row in result.get("rows", [])]

    meta = sql(f"SELECT sha256, complete, chunk_count FROM library_asset WHERE asset_id = '{quoted}'")
    if not meta:
        raise SystemExit(f"No asset {asset_id} in {db}")
    digest, complete, count = meta[0]
    if not complete:
        raise SystemExit(f"{asset_id} is still uploading")
    rows = sql(f"SELECT seq, data_b64 FROM asset_chunk WHERE asset_id = '{quoted}'")
    data = b"".join(base64.b64decode(b64) for _, b64 in sorted(rows))
    if len(rows) != count or hashlib.sha256(data).hexdigest() != digest:
        raise SystemExit(f"{asset_id} failed its checksum. Do not use it.")
    return data


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("asset_id")
    p.add_argument("--db", default="hackutd-freeboard-library")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)
    data = fetch(args.asset_id, args.db)
    args.out.write_bytes(data)
    print(f"{args.asset_id}: {len(data)} bytes, checksum OK, saved to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
