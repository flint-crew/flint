"""Common prefect tasks around interacting with measurement sets"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from prefect import Task, task

from flint.imager.wsclean import WSCleanResult
from flint.logging import logger
from flint.ms import subtract_model_from_data_column
from flint.options import MS
from flint.peel.jolly import jolly_roger_tractor
from flint.predict.addmodel import AddModelOptions, add_model

P = ParamSpec("P")
R = TypeVar("R")

task_subtract_model_from_ms = task(subtract_model_from_data_column)


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


@task
def task_jolly_roger_tractor(
    ms: MS, update_tukey_tractor_options: dict[str, Any] | None = None
) -> MS:
    """Run the jolly-rogar tukey tractor operation

    Args:
        ms (MS): The MS to modified. The `column` attribute will be used and modified inplace.
        update_tukey_tractor_options (dict[str, Any] | None, optional): Any additional options to provide the tukey tractor. Column related options are updated based on input `ms.column` attribute. Defaults to None.

    Returns:
        MS: Reference to modified measurement set
    """

    update_tukey_tractor_options = (
        update_tukey_tractor_options if update_tukey_tractor_options else {}
    )
    data_column = ms.column

    logger.info(f"Updating tukey tractor options to modified {data_column=}")
    update_tukey_tractor_options["data_column"] = data_column
    update_tukey_tractor_options["output_column"] = data_column

    return jolly_roger_tractor(
        ms=ms, update_tukey_tractor_options=update_tukey_tractor_options
    )
