"""Read a blueprint file from the command line.

    .venv/bin/python -m ingest.cli path/to/site_plan.png --out blueprint.json
"""
import argparse
import json
import sys

from . import IngestError, ingest_bytes


def main(argv=None):
    parser = argparse.ArgumentParser(description="Turn a site plan into blueprint.json")
    parser.add_argument("file")
    parser.add_argument("--out", help="write the blueprint here")
    args = parser.parse_args(argv)
    with open(args.file, "rb") as handle:
        data = handle.read()
    try:
        result = ingest_bytes(data, args.file)
    except IngestError as exc:
        print(f"Could not read {args.file}: {exc.message}", file=sys.stderr)
        return 1
    print(f"Read by: {result['source']['read_by']}  Counts: {result['counts']}")
    for item in result["review"]:
        print(f"  review {item['id']}: {item['issue']}")
    for err in result["errors"]:
        print(f"  ERROR {err}")
    if args.out:
        with open(args.out, "w") as handle:
            json.dump(result["blueprint"], handle, indent=2)
        print(f"Wrote {args.out}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
