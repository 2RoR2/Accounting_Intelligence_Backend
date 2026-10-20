"""Check the unchanged snapshot and run every original M2 test.

Run with Python 3.12 and requirements-test.txt; no model or server is needed.
"""
import hashlib
import json
from pathlib import Path
import sys
import unittest


def main():
    sys.dont_write_bytecode = True
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8-sig"))
    for entry in manifest["files"]:
        path = root / entry["path"]
        raw = path.read_bytes()
        if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise SystemExit(f"Baseline snapshot changed: {entry['path']}")
    print(f"SHA-256 verified: {len(manifest['files'])} unchanged files", flush=True)
    suite = unittest.defaultTestLoader.discover(str(root / "project/tests"), pattern="test_*.py")
    count = suite.countTestCases()
    if count != manifest["expected_test_count"]:
        raise SystemExit(f"Expected {manifest['expected_test_count']} original tests; discovered {count}")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
