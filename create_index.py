#!/usr/bin/python3

import json
import sys
from pathlib import Path


def collect_tags(obj, tags):
    """
    Recursively traverse a JSON structure.
    Collect tags only from objects where:
        "pub_status": "published"
    """
    if isinstance(obj, dict):
        if obj.get("pub_status") == "published" and "tags" in obj:
            value = obj["tags"]
            if isinstance(value, list):
                tags.update(str(item) for item in value)
            else:
                tags.add(str(value))

        for value in obj.values():
            collect_tags(value, tags)

    elif isinstance(obj, list):
        for item in obj:
            collect_tags(item, tags)


def main():
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("cache/notes")

    if not directory.is_dir():
        print(f"Error: '{directory}' is not a directory", file=sys.stderr)
        sys.exit(1)

    json_files = sorted(
        f for f in directory.iterdir()
        if f.is_file()
        and f.suffix.lower() == ".json"
    )

    index = []

    for json_file in json_files:
        try:
            with json_file.open("r", encoding="utf-8") as f:
                data = json.load(f)

            tags = set()
            collect_tags(data, tags)

            if tags:
                index.append({
                    "notes": f"{directory.name}/{json_file.name}",
                    "tags": sorted(tags)
                })

        except Exception as e:
            print(f"Warning: failed to process {json_file}: {e}", file=sys.stderr)

    output_file = "cache/index.json"

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

    print(f"Wrote {output_file}")


if __name__ == "__main__":
    main()
