from __future__ import annotations

import os
import shutil
import time
from pathlib import Path


_MANAGED_TEMP_ROOT: Path | None = None


def pytest_configure() -> None:
    global _MANAGED_TEMP_ROOT
    if "PYTEST_DEBUG_TEMPROOT" in os.environ:
        return
    root = Path(__file__).resolve().parents[2] / "tmp" / f"p{os.getpid()}"
    root.mkdir(parents=True, exist_ok=False)
    if os.name == "nt":
        local_state = root / "local"
        local_state.mkdir()
        os.environ["LOCALAPPDATA"] = str(local_state)
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(root)
    _MANAGED_TEMP_ROOT = root


def pytest_sessionfinish() -> None:
    if _MANAGED_TEMP_ROOT is None:
        return
    last_error: OSError | None = None
    for attempt in range(10):
        try:
            shutil.rmtree(_MANAGED_TEMP_ROOT)
            last_error = None
            break
        except OSError as exc:
            last_error = exc
            time.sleep(0.05 * (attempt + 1))
    if last_error is not None:
        raise last_error
    parent = _MANAGED_TEMP_ROOT.parent
    try:
        parent.rmdir()
    except OSError:
        pass
