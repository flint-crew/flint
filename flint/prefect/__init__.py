from __future__ import annotations

import os

# prefect 3 caches task results by default (INPUTS + TASK_SOURCE + RUN_ID). flint's
# tasks are side-effecting, so a cache hit is a skipped side effect, not a saved
# computation.
os.environ.setdefault("PREFECT_TASKS_DEFAULT_NO_CACHE", "true")
