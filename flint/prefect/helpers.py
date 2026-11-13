"""Assorted bits and pieces to help with prefect interactions.

Not intended to contain any common task decorated functions
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from prefect import get_run_logger


# This function has been lifted from
# https://gist.github.com/anna-geller/0b9e6ecbde45c355af425cd5b97e303d
# like any good pirate why make when you can take
def enable_loguru_support() -> None:
    """Redirect loguru logging messages to the prefect run logger.
    This function should be called from within a Prefect task or flow before calling any module that uses loguru.
    This function can be safely called multiple times.

    Example Usage:

    >>> from prefect import flow
    >>> from loguru import logger
    >>> from prefect_utils import enable_loguru_support # import this function in your flow from your module
    >>> @flow()
    >>> def myflow():
    >>>     logger.info("This is hidden from the Prefect UI")
    >>>     enable_loguru_support()
    >>>     logger.info("This shows up in the Prefect UI")

    """
    # import here for distributed execution because loguru cannot be pickled.
    from loguru import logger  # pylint: disable=import-outside-toplevel

    run_logger = get_run_logger()
    logger.remove()
    log_format = "{name}:{function}:{line} - {message}"
    logger.add(
        run_logger.debug,
        filter=lambda record: record["level"].name == "DEBUG",
        level="TRACE",
        format=log_format,
    )
    logger.add(
        run_logger.warning,
        filter=lambda record: record["level"].name == "WARNING",
        level="TRACE",
        format=log_format,
    )
    logger.add(
        run_logger.error,
        filter=lambda record: record["level"].name == "ERROR",
        level="TRACE",
        format=log_format,
    )
    logger.add(
        run_logger.critical,
        filter=lambda record: record["level"].name == "CRITICAL",
        level="TRACE",
        format=log_format,
    )
    logger.add(
        run_logger.info,
        filter=lambda record: (
            record["level"].name not in ["DEBUG", "WARNING", "ERROR", "CRITICAL"]
        ),
        level="TRACE",
        format=log_format,
    )


def _scheduler_state(dask_scheduler: Any) -> dict[str, Any]:
    """What the dask scheduler holds of its tasks and workers"""
    from time import time

    states: dict[str, int] = {}
    waiting = []
    for task in dask_scheduler.tasks.values():
        states[task.state] = states.get(task.state, 0) + 1
        if task.state in ("waiting", "queued", "no-worker"):
            waiting.append(f"{task.key} ({task.state})")
    workers = [
        {
            "name": str(worker.name),
            "status": worker.status.name,
            "last_seen": round(time() - worker.last_seen, 1),
            "processing": [str(task.key) for task in worker.processing],
            "long_running": len(worker.long_running),
        }
        for worker in dask_scheduler.workers.values()
    ]
    return {"states": states, "waiting": waiting[:20], "workers": workers}


@contextmanager
def watch_dask_cluster(interval: float = 60.0) -> Iterator[None]:
    """Log what the dask cluster of this flow is doing, to see where a stall is"""
    from prefect.context import FlowRunContext

    flow_run_context = FlowRunContext.get()
    assert flow_run_context is not None, "The dask cluster is watched from its flow"
    client = flow_run_context.task_runner.client  # type: ignore[attr-defined]
    run_logger = get_run_logger()
    stop = threading.Event()

    def report(client: Any) -> None:
        while not stop.wait(interval):
            try:
                state = client.run_on_scheduler(_scheduler_state)
                run_logger.info(f"Cluster watch: tasks {state['states']}")
                if state["waiting"]:
                    run_logger.info(f"Cluster watch: not started {state['waiting']}")
                for worker in state["workers"]:
                    run_logger.info(f"Cluster watch: worker {worker}")
                # A worker whose event loop is stuck does not answer this
                stacks = client.sync(
                    client.scheduler.call_stack, keys=None, callback_timeout=20
                )
                for address, keys in stacks.items():
                    for key, frames in keys.items():
                        where = " <- ".join(
                            frame.strip().splitlines()[0] for frame in frames[-4:][::-1]
                        )
                        run_logger.info(f"Cluster watch: {address} {key} at {where}")
            except Exception as error:
                run_logger.warning(f"Cluster watch failed: {error!r}")

    thread = threading.Thread(target=report, args=(client,), daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=30)
