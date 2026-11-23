"""Plan M B7: the Performance evaluation page reads the latest lite-extraction benchmark (read-only).

`scripts/lite_benchmark.py` writes `runs/lite-benchmark-<UTC>.json`. This returns the newest completed one (not
stopped, with documents), so the page shows current evidence only. The result is synthetic evaluation data, not a
company's documents; local file paths are reduced to paths relative to the repository so no machine path leaks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from main import HTTPException, Request, require_workspace_user

ROOT = Path(__file__).resolve().parents[3]
ROLES = {"super_admin", "local_admin", "accountant"}


def runs_dir() -> Path:
    return Path(os.environ.get("EVALUATION_RUNS_DIR") or ROOT / "runs")


def _relative(value):
    if not isinstance(value, str) or not value:
        return value
    path = Path(value)
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return path.name if path.is_absolute() else value


def latest_result(folder: Path | None = None) -> dict | None:
    folder = folder or runs_dir()
    for path in sorted(folder.glob("lite-benchmark-*.json"), reverse=True):  # UTC stamp in the name sorts by time
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(result, dict) or result.get("stopped") or not result.get("documents"):
            continue
        settings = dict(result.get("settings") or {})
        for key, value in list(settings.items()):
            if key.endswith(("_dir", "_path", "dir", "path")):
                settings[key] = _relative(value)
        result["settings"] = settings
        result["file"] = path.name
        return result
    return None


def latest_evaluation(request: Request):
    require_workspace_user(request, ROLES)
    result = latest_result()
    if result is None:
        raise HTTPException(404, "No completed benchmark result yet.")
    return result


def register(router):
    router.add_api_route('/api/evaluation/latest', latest_evaluation, methods=['GET'], tags=['Reports'])
