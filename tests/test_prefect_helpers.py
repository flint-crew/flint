"""Basic tests around prefect helper functions"""

from __future__ import annotations

from prefect import flow
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
