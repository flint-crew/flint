"""Basic tests around prefect helper functions"""

from __future__ import annotations

from pathlib import Path

from prefect import flow, task
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness

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


@task
def _append_marker(marker: Path, name: str) -> str:
    with marker.open("a") as file:
        file.write(f"{name}\n")
    return name


@flow
def _flow_repeating_a_task(marker: Path) -> None:
    _append_marker.submit(marker, "beam00").result()
    _append_marker.submit(marker, "beam00").result()


def test_side_effect_not_repeated_within_flow_run(tmp_path):
    """A killed dask worker makes dask recompute the tasks whose results it held,
    which for flint means repeating a side effect. Persisted results turn that
    repeat into a lookup, while the flow run id in the prefect cache policy keeps
    a later flint run re-executing rather than skipping the work."""

    marker = tmp_path / "side_effects.txt"
    marker.touch()

    with prefect_test_harness(), disable_run_logger():
        _flow_repeating_a_task(marker)
        assert marker.read_text().split() == ["beam00"]

        # A new flow run has a new run id, so the side effect is not skipped
        _flow_repeating_a_task(marker)
        assert marker.read_text().split() == ["beam00", "beam00"]
