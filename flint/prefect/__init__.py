from __future__ import annotations

import os

# Has to be set before anything imports `prefect`, which reads its settings once at
# import time and ignores later changes to the environment. This package `__init__` is
# the only place that reliably runs first, as importing any `flint.prefect` submodule
# initialises it before running the submodule (and its own `import prefect`).
#
# Task results are persisted so that a `dask` worker death becomes a lookup rather than
# a recomputed side effect - see `flint.prefect.caching`. Results are written to
# $PREFECT_HOME/storage, which every worker has to be able to read; set
# PREFECT_LOCAL_STORAGE_PATH if $HOME is not shared across the cluster.
os.environ.setdefault("PREFECT_RESULTS_PERSIST_BY_DEFAULT", "true")
