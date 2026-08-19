"""Basic tests around prefect helper functions"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from prefect import flow, task
from prefect.logging import disable_run_logger
from prefect.task_engine import run_task_sync
from prefect.testing.utilities import prefect_test_harness

from flint.prefect.caching import task as flint_task
from flint.prefect.helpers import enable_loguru_support


@flow
def example_flow():
    enable_loguru_support()
    return "JackSparrow"


def test_enable_loguru_support():
    """Some packages may be using loguru (e.g. crystalball). Should
    we want those logs to be captured we need to modify the loguru
    logger. A helpful function has been added to this end. This
    is a simple, very basic test to make sure it can still run without
    error, though whether it still works is a completely different
    question!"""

    with prefect_test_harness(), disable_run_logger():
        assert example_flow() == "JackSparrow"


@task
def _boom() -> None:
    raise ValueError("kraken")


@flow
def _flow_returning_futures():
    return [_boom.submit()]


def test_failed_task_fails_flow():
    """Prefect only fails a flow on a task failure if that task's future is
    part of the flow return value. The flows all return their terminal futures
    for this reason."""

    with prefect_test_harness(), disable_run_logger():
        assert _flow_returning_futures(return_state=True).is_failed()


@flint_task
def _append_marker(marker: Path, name: str) -> str:
    with marker.open("a") as file:
        file.write(f"{name}\n")
    return name


@flow
def _flow_submitting_a_task_twice(marker: Path) -> None:
    _append_marker.submit(marker, "beam00").result()
    _append_marker.submit(marker, "beam00").result()


@flow
def _flow_recomputing_a_task_run(marker: Path) -> None:
    """Re-invoke one task run twice, as dask does when it recomputes a task whose
    worker was killed - same task run id, same parameters"""

    task_run_id = uuid4()
    for _ in range(2):
        run_task_sync(
            task=_append_marker,
            task_run_id=task_run_id,
            parameters={"marker": marker, "name": "beam00"},
            return_type="result",
        )


def test_recompute_is_a_lookup_but_a_resubmit_is_not(tmp_path):
    """A killed dask worker makes dask recompute the tasks whose results it held,
    which for flint means repeating a side effect. `flint.prefect.task` keys the
    result on the task run id so that recompute is a lookup, while a task flint
    genuinely submits twice still runs twice - which the prefect default policy,
    keyed on the inputs and the flow run id, gets wrong."""

    marker = tmp_path / "side_effects.txt"
    marker.touch()

    with prefect_test_harness(), disable_run_logger():
        _flow_recomputing_a_task_run(marker)
        assert marker.read_text().split() == ["beam00"]

        marker.write_text("")
        _flow_submitting_a_task_twice(marker)
        assert marker.read_text().split() == ["beam00", "beam00"]
