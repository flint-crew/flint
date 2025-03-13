"""Common prefect related utilities that can be used between flows."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Callable, Iterable, ParamSpec, TypeVar
from uuid import UUID

from prefect import task
from prefect.artifacts import create_markdown_artifact

from flint.archive import copy_sbid_files_archive, create_sbid_tar_archive
from flint.logging import logger
from flint.naming import (
    add_timestamp_to_path,
    get_fits_cube_from_paths,
    get_sbid_from_path,
    rename_linear_to_stokes,
)
from flint.options import ArchiveOptions
from flint.summary import (
    create_beam_summary,
    create_field_summary,
    update_field_summary,
)

T = TypeVar("T")
P = ParamSpec("P")
R = TypeVar("R")
C = TypeVar("C", bound=Callable)

SUPPORTED_IMAGE_TYPES = ("png",)


@task
def task_create_object(object: C, **kwargs) -> C:
    return object(**kwargs)


@task
def task_getattr(
    item: object,
    attribute: str,
    /,
) -> Any:
    """Retrieve an attribute from an input instance of a class or structure.

    Args:
        item (Any): The item that has the input class or structure
        attribute (str): The attribute to extract

    Returns:
        Any: Value of the requested attribute
    """
    logger.debug(f"Pulling {attribute=}")
    return getattr(item, attribute)


@task
def task_sorted(
    iterable: Iterable[T],
    /,
    *,
    key: Any = None,
    reverse: bool = False,
) -> list[T]:
    return sorted(iterable, key=key, reverse=reverse)


@task
def task_zip_list_of_list(list_of_list: list[list[T]]) -> list[tuple[T, ...]]:
    return list(zip(*list_of_list))


def upload_image_as_artifact(
    image_path: Path, description: str | None = None
) -> UUID | None:
    """Create and submit a markdown artifact tracked by prefect for an
    input image. Currently supporting png formatted images.

    The input image is converted to a base64 encoding, and embedded directly
    within the markdown string. Therefore, be mindful of the image size as this
    is tracked in the postgres database.

    Args:
        image_path (Path): Path to the image to upload
        description (Optional[str], optional): A description passed to the markdown artifact. Defaults to None.

    Returns:
        UUID | None: Generated UUID of the registered artifact. If the upload fails ``None`` is returned.
    """
    from prefect.exceptions import PrefectHTTPStatusError

    image_type = image_path.suffix.replace(".", "")
    assert image_path.exists(), f"{image_path} does not exist"
    assert image_type in SUPPORTED_IMAGE_TYPES, (
        f"{image_path} has type {image_type}, and is not supported. Supported types are {SUPPORTED_IMAGE_TYPES}"
    )

    with open(image_path, "rb") as open_image:
        logger.info(f"Encoding {image_path} in base64")
        image_base64 = base64.b64encode(open_image.read()).decode()

    logger.info("Creating markdown tag")
    markdown = f"![{image_path.stem}](data:image/{image_type};base64,{image_base64})"

    try:
        image_uuid: UUID | None = None
        logger.info("Registering artifact")
        image_uuid = create_markdown_artifact(
            markdown=markdown, description=description
        )
    except PrefectHTTPStatusError as e:
        logger.warning("Failed to register artefact. ")
        logger.warning(f"{e=}")

    return image_uuid


task_update_field_summary = task(update_field_summary)
task_create_field_summary = task(create_field_summary)
task_create_beam_summary = task(create_beam_summary)
task_get_fits_cube_from_paths = task(get_fits_cube_from_paths)
task_rename_linear_to_stokes = task(rename_linear_to_stokes)


@task
def task_archive_sbid(
    science_folder_path: Path,
    archive_path: Path | None = None,
    copy_path: Path | None = None,
    max_round: int | None = None,
    update_archive_options: dict[str, Any] | None = None,
) -> Path:
    """Create a tarball of files, or copy files, from a processing folder.

    Args:
        science_folder_path (Path): Path that contains the imaged produced
        archive_path (Optional[Path], optional): Location to create and store the tar ball at. If None no tarball is created. Defaults to None.
        copy_path (Optional[Path], optional): Location to copy selected files into. If None no files are copied. Defaults to None.
        max_round (Optional[int], optional): The last self-calibration round performed. If provided some files form this round are copied (assuming wsclean imaging). If None, the default file patterns in ArchiveOptions are used. Defaults to None.
        update_archive_options (Optional[Dict[str, Any]], optional): Additional options to provide to ArchiveOptions. Defaults to None.

    Returns:
        Path: The science folder files were copied from
    """

    sbid = get_sbid_from_path(path=science_folder_path)

    archive_options = ArchiveOptions()

    if update_archive_options:
        logger.info(f"Updating archive options with {update_archive_options=}")
        archive_options = archive_options.with_options(**update_archive_options)

    # TODO: What should this be? Just general new regexs passed through,
    # or is this fine?
    if max_round:
        updated_file_patterns = (
            *tuple(archive_options.tar_file_re_patterns),
            f".*beam[0-9]+\\.round{max_round}-.*-image\\.fits",
            f".*beam[0-9]+\\.round{max_round}\\.ms\\.(zip|tar)",
        )
        archive_options = archive_options.with_options(
            tar_file_re_patterns=updated_file_patterns
        )

    if archive_path:
        tar_file_name = add_timestamp_to_path(Path(archive_path) / f"SB{sbid}.tar")
        create_sbid_tar_archive(
            tar_out_path=tar_file_name,
            base_path=science_folder_path,
            archive_options=archive_options,
        )

    if copy_path:
        copy_sbid_files_archive(
            copy_out_path=copy_path,
            base_path=science_folder_path,
            archive_options=archive_options,
        )

    return science_folder_path


@task
def task_update_with_options(input_object: T, **kwargs) -> T:
    """Updated any object via its `.with_options()` interface.

    All key-word arguments other than `input_object` are passed through
    to that `input_object`s `.with_options()` method.

    Args:
        input_object (T): The object that has an `.with_options` method that will be updated

    Returns:
        T: The updated object
    """
    assert "with_options" in dir(input_object), (
        f"{type(input_object)=} does not have a with_options method"
    )
    updated_object = input_object.with_options(**kwargs)  # type: ignore

    return updated_object


@task
def task_get_attributes(item: Any, attribute: str) -> Any:
    """Retrieve an attribute from an input instance of a class or structure.

    This is intended to be used when dealing with a prefect future object that
    has yet to be evaluated or is otherwise not immediately accessible.

    Args:
        item (Any): The item that has the input class or structure
        attribute (str): The attribute to extract

    Returns:
        Any: Value of the requested attribute
    """
    logger.debug(f"Pulling {attribute=}")
    return item.__dict__[attribute]


@task
def task_flatten(to_flatten: list[list[T]]) -> list[T]:
    """Will flatten a list of lists into a single list. This
    is useful for when a task-descorated function returns a list.


    Args:
        to_flatten (List[List[T]]): Input list of lists to flatten

    Returns:
        List[T]: Flattened form of input
    """
    logger.debug(f"Received {len(to_flatten)} to flatten.")

    flatten_list = [item for sublist in to_flatten for item in sublist]

    logger.debug(f"Flattened list {len(flatten_list)}")

    return flatten_list
