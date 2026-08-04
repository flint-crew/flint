from __future__ import annotations

import os

# When a dask worker dies the results it held are lost, and dask recomputes the tasks
# that made them - which for flint means repeating a side effect (recopying an MS,
# reapplying a solution), usually onto outputs that already exist. Persisting results
# lets prefect return the completed result instead of re-running the task.
#
# prefect's default cache policy is INPUTS + TASK_SOURCE + RUN_ID, so a hit is only
# possible within the same flow run. A new flint run has a new RUN_ID and so always
# re-executes - a cache hit never silently skips a side effect across runs.
#
# Results are written to $PREFECT_HOME/storage, which every worker has to be able to
# read. Set PREFECT_LOCAL_STORAGE_PATH if $HOME is not shared across the cluster.
os.environ.setdefault("PREFECT_RESULTS_PERSIST_BY_DEFAULT", "true")
