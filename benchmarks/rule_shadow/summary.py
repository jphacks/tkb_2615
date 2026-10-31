"""Add up what benchmarks/rule_shadow/sitecustomize.py saw: python summary.py DIR."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    seen: Counter[str] = Counter()
    unsupported: Counter[str] = Counter()
    differences: list[str] = []
    processes = 0
    for path in sorted(Path(sys.argv[1]).glob("*.json")):
        document = json.loads(path.read_text("utf-8"))
        processes += 1
        seen.update(document["seen"])
        unsupported.update(document["unsupported"])
        differences += document["differences"]
    print(f"processes: {processes}")
    print("seen:", json.dumps(seen, sort_keys=True))
    print("unsupported:", json.dumps(unsupported, sort_keys=True, ensure_ascii=False))
    print(f"differences: {len(differences)}")
    for difference in differences[:20]:
        print(" ", difference)
    return 1 if differences else 0


if __name__ == "__main__":
    raise SystemExit(main())
