"""Common prefect tasks around interacting with measurement sets"""

from __future__ import annotations

from pathlib import Path
from typing import ParamSpec, TypeVar

from prefect import Task, task

from flint.calibrate.aocalibrate import AddModelOptions, add_model
from flint.imager.wsclean import WSCleanResult
from flint.logging import logger
from flint.ms import describe_ms

task_describe_ms = task(describe_ms)

P = ParamSpec("P")
R = TypeVar("R")


# TODO: This can be a dispatcher type function should
# other modes be added
def add_model_source_list_to_ms(
    wsclean_command: WSCleanResult, calibrate_container: Path | None = None
) -> WSCleanResult:
    logger.info("Updating MODEL_DATA with source list")
    ms = wsclean_command.ms

    assert wsclean_command.image_set is not None, (
        f"{wsclean_command.image_set=}, which is not allowed"
    )

    source_list_path = wsclean_command.image_set.source_list
    if source_list_path is None:
        logger.info(f"{source_list_path=}, so not updating")
        return wsclean_command
    assert source_list_path.exists(), f"{source_list_path=} does not exist"

    if calibrate_container is None:
        logger.info(f"{calibrate_container=}, so not updating")
        return wsclean_command
    assert calibrate_container.exists(), f"{calibrate_container=} does not exist"

    add_model_options = AddModelOptions(
        model_path=source_list_path,
        ms_path=ms.path,
        mode="c",
        datacolumn="MODEL_DATA",
    )
    add_model(
        add_model_options=add_model_options,
        container=calibrate_container,
        remove_datacolumn=True,
    )
    return wsclean_command


task_add_model_source_list_to_ms: Task[P, R] = task(add_model_source_list_to_ms)
