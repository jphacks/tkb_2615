"""Compare sheltersim_native.canonical_json with Python's canonical JSON, then time both.

Usage: python native/tests/parity_check.py [PROJECTS_DIR BRANCH]

With a projects directory (from benchmarks/investigate_performance.py --route
project), the branch's head Checkpoint documents are compared and timed too.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import struct
import sys
import time
from pathlib import Path

import sheltersim_native as native


def python_canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def check_floats() -> None:
    rng = random.Random(2026)
    floats = [0.0, -0.0, 1.0, -1.0, 1e16, 1e15, 9999999999999998.0, 1e-4, 1e-5, 0.1, 0.3]
    floats += [2.5e-7, 5e-324, 2.2250738585072014e-308, 1.7976931348623157e308]
    floats += [123456789012345680.0, 1e22, 1e21, 1e23]
    floats += [
        struct.unpack("<d", struct.pack("<Q", rng.getrandbits(64)))[0] for _ in range(300_000)
    ]
    floats += [rng.uniform(-1e6, 1e6) for _ in range(200_000)]
    floats += [rng.random() * 10 ** rng.randint(-30, 30) for _ in range(200_000)]
    floats += [round(rng.random(), rng.randint(0, 12)) for _ in range(100_000)]
    floats = [value for value in floats if math.isfinite(value)]
    mismatches = [value for value in floats if native.float_repr(value) != repr(value)]
    print("floats checked", len(floats), "mismatches", len(mismatches), mismatches[:5])


def check_structures() -> None:
    samples = ["", 'a"b\\c', "\n\r\t\b\f", "\x00\x01\x1f\x7f", "避難所", "😀", "  "]
    documents = [
        {"b": 1, "a": [1, 2.5, None, True, False, "x"], "c": {"z": {"y": []}}},
        {sample: sample for sample in samples},
        [10**30, -(10**30), 2**63, -(2**63) - 1],
        (1, 2, (3,)),
        {"é": 1, "e": 2, "😀": 3, "￿": 4, "a": 5},
    ]
    for document in documents:
        assert native.canonical_json(document) == python_canonical(document), document
    for value in [float("nan"), {1: 2}, {"a": set()}, b"x"]:
        try:
            native.canonical_json(value)
        except native.Unsupported:
            continue
        raise AssertionError(f"accepted a value json would handle differently: {value!r}")
    print("structures and unsupported values ok")


def check_checkpoint(projects: Path, branch: str) -> None:
    os.environ["SHELTERSIM_LLM_MODE"] = "offline"
    from sheltersim.storage.projects import ProjectStore

    store = ProjectStore(projects, max_bytes=100_000_000_000)
    reference = store.branch_reference("p", "r", branch)
    checkpoint = store.load_checkpoint("p", "r", reference.checkpoint_hash)
    for name in ("world_state", "metrics_state"):
        document = getattr(checkpoint, name)
        started = time.perf_counter()
        expected = python_canonical(document)
        python_seconds = time.perf_counter() - started
        started = time.perf_counter()
        actual = native.canonical_json(document)
        native_seconds = time.perf_counter() - started
        same_digest = hashlib.sha256(expected).hexdigest() == native.canonical_sha256(document)
        print(
            f"{name}: {len(expected) / 1e6:.1f} MB equal={expected == actual} "
            f"digest_equal={same_digest} python {python_seconds * 1000:.0f} ms "
            f"native {native_seconds * 1000:.0f} ms"
        )


if __name__ == "__main__":
    check_floats()
    check_structures()
    if len(sys.argv) == 3:
        check_checkpoint(Path(sys.argv[1]), sys.argv[2])
