"""Utilities related to using casa tasks"""

from __future__ import annotations

from pathlib import Path

from flint.logging import logger
from flint.sclient import singularity_wrapper
from flint.utils import remove_files_folders


def args_to_casa_task_string(task: str, **kwargs) -> str:
    """Given a set of arguments, convert them to a string that can
    be used to run the corresponding CASA task that can be passed
    via ``casa -c`` for execution

    Args:
        task (str): The name of the task that will be executed

    Returns:
        str: The formatted string that will be given to CASA for execution
    """
    command = []
    for k, v in kwargs.items():
        if isinstance(v, (list, tuple)):
            v = ",".join(rf"'{_v!s}'" for _v in v)
            arg = rf"{k}=({v})"
        elif isinstance(v, (str, Path)):
            arg = rf"{k}='{v!s}'"
        else:
            arg = rf"{k}={v}"
        command.append(arg)

    task_command = rf"casa -c {task}(" + ",".join(command) + r")"

    return task_command


# TODO There should be a general casa_command type function that accepts the task as a keyword
# so that each casa task does not need an extra function


@singularity_wrapper
def mstransform(**kwargs) -> str:
    """Construct and run CASA's ``mstransform`` task.

    Args:
        casa_container (Path): Container with the CASA tooling
        ms (str): Path to the measurement set to transform
        output_ms (str): Path of the output measurement set produced by the transform

    Returns:
        str: The ``mstransform`` string
    """
    mstransform_str = args_to_casa_task_string(task="mstransform", **kwargs)
    logger.info(f"{mstransform_str=}")

    return mstransform_str


@singularity_wrapper
def cvel(**kwargs) -> str:
    """Generate the CASA cvel command

    Returns:
        str: The command to execute
    """
    cvel_str = args_to_casa_task_string(task="cvel", **kwargs)
    logger.info(f"{cvel_str=}")

    return cvel_str


@singularity_wrapper
def applycal(**kwargs) -> str:
    """Generate the CASA applycal command

    Returns:
        str: The command to execute
    """
    applycal_str = args_to_casa_task_string(task="applycal", **kwargs)
    logger.info(f"{applycal_str=}")

    return applycal_str


@singularity_wrapper
def gaincal(**kwargs) -> str:
    """Generate the CASA gaincal command

    Returns:
        str: The command to execute
    """
    gaincal_str = args_to_casa_task_string(task="gaincal", **kwargs)
    logger.info(f"{gaincal_str=}")

    return gaincal_str


def copy_with_mstranform(casa_container: Path, ms_path: Path) -> Path:
    """Use the casa task mstransform to create `nspw` spectral windows
    in the input measurement set. This is necessary when attempting to
    use gaincal to solve for some frequency-dependent solution.

    Args:
        casa_container (Path): Path to the singularity container with CASA tooling
        ms_path (Path): The measurement set that should be reformed to have `nspw` spectral windows

    Returns:
        Path: The path to the measurement set that was updated
    """

    transform_ms = ms_path.with_suffix(".ms_transform")

    mstransform(
        container=casa_container,
        bind_dirs=(ms_path.parent, transform_ms.parent),
        vis=str(ms_path),
        outputvis=str(transform_ms),
        createmms=False,
        datacolumn="all",
    )

    logger.info(f"Successfully created the transformed measurement set {transform_ms}.")
    remove_files_folders(ms_path)

    logger.info(f"Renaming {transform_ms} to {ms_path}.")
    transform_ms.rename(ms_path)

    return ms_path
