"""The `task` decorator that `flint` tasks are built from, and the cache policy that
makes a `dask` worker death survivable.

When a `dask` worker dies the results it held are lost, and `dask` recomputes the tasks
that made them - including tasks that had already completed. For `flint` that means
repeating a side effect (recopying an MS, reapplying a solution) onto outputs that
already exist, which usually fails. Persisted results let `prefect` look the result up
instead, so long as the cache policy can recognise a recompute for what it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Any

from prefect import task as _prefect_task
from prefect.cache_policies import CachePolicy
from prefect.context import TaskRunContext


@dataclass
class TaskRunId(CachePolicy):
    """Key a task result on the id of the task run that produced it.

    `prefect_dask` mints the task run id on the client and passes it into the `dask`
    submission, so a task `dask` recomputes after a worker death carries the id it had
    on its first attempt, while every fresh submission gets a new one. Keying on it
    means a lost result is looked up rather than recomputed, and a task `flint`
    intentionally runs twice is never mistaken for a recompute - which the `prefect`
    default policy (inputs, task source and *flow* run id) cannot distinguish.

    Only completed work is protected. The task that actually died committed no result
    and so runs again, meaning tasks still have to tolerate their own partial output.
    """

    def compute_key(
        self,
        task_ctx: TaskRunContext,
        inputs: dict[str, Any],
        flow_parameters: dict[str, Any],
        **kwargs: Any,
    ) -> str | None:
        return str(task_ctx.task_run.id) if task_ctx else None


task = partial(_prefect_task, cache_policy=TaskRunId())
"""`prefect.task`, keyed so only a `dask` recompute of the same task run hits the cache"""
