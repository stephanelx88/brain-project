"""Hook latency budget — see spec §4.4."""
from __future__ import annotations

import os
import statistics
import subprocess
import sys
import time

import pytest


def _run_hook_python_only(env: dict) -> float:
    start = time.perf_counter()
    subprocess.run(
        [sys.executable, "-m", "brain.runtime.hook"],
        env=env, check=True, capture_output=True,
    )
    return (time.perf_counter() - start) * 1000  # ms


@pytest.mark.skipif(
    sys.platform == "win32", reason="latency budget tuned for unix; skip on win"
)
def test_empty_path_python_module_under_p99_budget(tmp_path):
    env = os.environ.copy()
    env["BRAIN_RUNTIME_DIR"] = str(tmp_path)
    env.pop("CLAUDE_SESSION_ID", None)

    runs = [_run_hook_python_only(env) for _ in range(20)]
    median = statistics.median(runs)
    p99 = sorted(runs)[-1]

    # Python cold-start dominates the empty-path call. Generous budget so
    # this passes on slow CI; the shell wrapper (compgen-based fast path)
    # provides the tighter real-world bound when the inbox is empty.
    assert median <= 1500, f"median={median:.1f}ms exceeds 1500ms budget; got runs={runs}"
    assert p99 <= 2500, f"p99={p99:.1f}ms exceeds 2500ms budget; got runs={runs}"
