"""This contains common utilities to enable components of the prefect imaging flowws.
The majority of the items here are the task decorated functions. Effort should be made
to avoid putting in too many items that are not going to be directly used by prefect
imaging flows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Collection, Literal, ParamSpec, TypeVar

import numpy as np
import pandas as pd
from prefect import Task, task, unmapped
from prefect.artifacts import create_table_artifact

from flint.calibrate.aocalibrate import (
    ApplySolutions,
    CalibrateCommand,
    create_apply_solutions_cmd,
    select_aosolution_for_ms,
)
from flint.coadd.linmos import LinmosOptions, LinmosResult, linmos_images
from flint.convol import (
    BeamShape,
    convolve_cubes,
    convolve_images,
    get_common_beam,
    get_cube_common_beam,
)
from flint.flagging import flag_ms_aoflagger
from flint.imager.wsclean import (
    ImageSet,
    WSCleanOptions,
    WSCleanResult,
    combine_images_to_cube,
    image_set_from_result,
    merge_image_sets,
    merge_image_sets_from_results,
    split_and_get_image_set,
    wsclean_imager,
)
from flint.logging import logger
from flint.masking import (
    MaskingOptions,
    create_snr_mask_from_fits,
    extract_beam_mask_from_mosaic,
)
from flint.ms import (
    MS,
    copy_and_preprocess_casda_askap_ms,
    preprocess_askap_ms,
    rename_column_in_ms,
    split_by_field,
)
from flint.naming import (
    FITSMaskNames,
    get_beam_resolution_str,
    get_fits_cube_from_paths,
)
from flint.options import FieldOptions, SubtractFieldOptions
from flint.peel.potato import potato_peel
from flint.prefect.common.utils import upload_image_as_artifact
from flint.selfcal.casa import gaincal_applycal_ms
from flint.source_finding.aegean import AegeanOutputs, run_bane_and_aegean
from flint.summary import FieldSummary
from flint.utils import (
    flatten_items,
    zip_folder,
)
from flint.validation import (
    ValidationTables,
    XMatchTables,
    create_validation_plot,
    create_validation_tables,
)

P = ParamSpec("P")
R = TypeVar("R")

# These are simple task wrapped functions and require no other modification
task_copy_and_preprocess_casda_askap_ms: Task[P, R] = task(
    copy_and_preprocess_casda_askap_ms
)
task_preprocess_askap_ms: Task[P, R] = task(preprocess_askap_ms)
task_split_by_field: Task[P, R] = task(split_by_field)
task_select_solution_for_ms: Task[P, R] = task(select_aosolution_for_ms)
task_create_apply_solutions_cmd: Task[P, R] = task(create_apply_solutions_cmd)
task_rename_column_in_ms: Task[P, R] = task(rename_column_in_ms)
task_convolve_images = task(convolve_images)
task_split_and_get_image_set = task(split_and_get_image_set)
task_image_set_from_result = task(image_set_from_result)
task_combine_images_to_cube = task(combine_images_to_cube)
task_merge_image_sets = task(merge_image_sets)
task_merge_image_sets_from_results = task(merge_image_sets_from_results)

# Tasks below are extracting componented from earlier stages, or are
# otherwise doing something important

FlagMS = TypeVar("FlagMS", MS, ApplySolutions)


@task
def task_get_channel_images_from_paths(paths: list[Path]) -> list[Path]:
    return [path for path in paths if "MFS" not in path.name]


@task
def task_potato_peel(
    ms: MS,
    potato_container: Path,
    update_potato_config_options: dict[str, Any] | None = None,
    update_potato_peel_options: dict[str, Any] | None = None,
    update_wsclean_options: dict[str, Any] | None = None,
) -> MS:
    logger.info(f"Attempting to peel {ms.path}")

    if update_wsclean_options is None:
        update_wsclean_options = {}

    wsclean_options = WSCleanOptions(**update_wsclean_options)

    initial_data_column = ms.column

    ms = potato_peel(
        ms=ms,
        potato_container=potato_container,
        update_potato_config_options=update_potato_config_options,
        update_potato_peel_options=update_potato_peel_options,
        image_options=wsclean_options,
    )

    post_data_column = ms.column

    logger.info(f"Initial potato data column: {initial_data_column}")
    logger.info(f"Post potato data column: {post_data_column}")

    if post_data_column != initial_data_column:
        logger.critical(f"{ms.path} data column has changed!")

    return ms


@task
def task_flag_ms_aoflagger(ms: FlagMS, container: Path) -> FlagMS:
    # Pirate believes the type ignore below is due to the decorated function and type alias
    extracted_ms = MS.cast(ms=ms)  # type: ignore

    extracted_ms = flag_ms_aoflagger(ms=extracted_ms, container=container)

    return ms


@task
def task_bandpass_create_apply_solutions_cmd(
    ms: MS, calibrate_cmd: CalibrateCommand, container: Path
) -> ApplySolutions:
    """Apply a ao-calibrate solutions file to the subject measurement set.

    Args:
        ms (MS): The measurement set that will have the solutions file applied
        calibrate_cmd (CalibrateCommand): A resulting ao-calibrate command and meta-data item
        container (Path): The container that can apply the ao-calibrate style solutions file to the measurement set

    Returns:
        ApplySolutions: The resulting apply solutions command and meta-data
    """
    return create_apply_solutions_cmd(
        ms=ms, solutions_file=calibrate_cmd.solution_path, container=container
    )


@task
def task_extract_solution_path(calibrate_cmd: CalibrateCommand) -> Path:
    """Extract the solution path from a calibrate command. This is often required when
    interacting with a ``PrefectFuture`` wrapped ``CalibrateCommand`` result.

    Args:
        calibrate_cmd (CalibrateCommand): The subject calibrate command the solution file will be extracted from

    Returns:
        Path: Path to the solution file
    """
    # TODO: What is actually using this task? Is it better to just pass through a
    # CalibrateCommand?
    # TODO: Use the get attribute task enabled function
    return calibrate_cmd.solution_path


@task
def task_run_bane_and_aegean(
    image: WSCleanResult | LinmosResult,
    aegean_container: Path,
    update_bane_options: dict[str, Any] | None = None,
    update_aegean_options: dict[str, Any] | None = None,
) -> AegeanOutputs:
    """Run BANE and Aegean against a FITS image.

    Notes:
        It has been noted that BANE can sometimes get caught in a interpolation error which haults execution.
        The ``timelimit_seconds`` will attempt to detect long runnings BANE processes and raise an error. The
        retry functionality of prefect should then restart the task. Since this task is pure (e.g. no last
        dataproducts, modification to data etc) simply restarting should be fine.

    Args:
        image (Union[WSCleanResult, LinmosResult]): The image that will be searched
        aegean_container (Path): Path to a singularity container containing BANE and aegean
        update_bane_options (dict[str, Any] | None, optional): Over-ride any default options of BANEOptions. If None defaults are used. Defaults to None.
        update_aegean_options (dict[str, Any] | None, optional): Over-ride any default options of AegeanOptions. If None defaults are used. Defaults to None.

    Raises:
        ValueError: Raised when ``image`` is not a supported type

    Returns:
        AegeanOutputs: Output BANE and aegean products, including the RMS and BKG images
    """
    if isinstance(image, WSCleanResult):
        assert image.image_set is not None, "Image set attribute unset. "
        image_paths = image.image_set.image

        logger.info(f"Have extracted image: {image_paths}")

        # For the moment, will only source find on an MFS image
        image_paths = [image for image in image_paths if ".MFS." in str(image)]
        assert len(image_paths) == 1, (
            "More than one image found after filter_str for MFS only images. "
        )
        # Get out the only path in the list.
        image_path = image_paths[0]
    elif isinstance(image, LinmosResult):
        logger.info("Will be running aegean on a linmos image")

        image_path = image.image_fits
        assert image_path.exists(), f"Image path {image_path} does not exist"
    else:
        raise ValueError(f"Unexpected type, have received {type(image)} for {image=}. ")

    aegean_outputs = run_bane_and_aegean(
        image=image_path,
        aegean_container=aegean_container,
        update_bane_options=update_bane_options,
        update_aegean_options=update_aegean_options,
    )

    return aegean_outputs


@task
def task_zip_ms(in_item: WSCleanResult) -> Path:
    """Zip a measurement set

    Args:
        in_item (WSCleanResult): The inpute item with a ``.ms`` attribute of type ``MS``.

    Returns:
        Path: Output path of the zipped measurement set
    """
    # TODO: This typing needs to be expanded
    ms = in_item.ms

    zipped_ms = zip_folder(in_path=ms.path)

    return zipped_ms


@task
def task_gaincal_applycal_ms(
    ms: MS | WSCleanResult,
    selfcal_round: int,
    casa_container: Path,
    update_gain_cal_options: dict[str, Any] | None = None,
    archive_input_ms: bool = False,
    skip_selfcal: bool = False,
    rename_ms: bool = False,
    archive_cal_table: bool = False,
) -> MS:
    """Perform self-calibration using CASA gaincal and applycal.

    Args:
        ms (Union[MS, WSCleanResult]): A resulting wsclean output. This is used purely to extract the ``.ms`` attribute.
        selfcal_round (int): Counter indication which self-calibration round is being performed. A name is included based on this.
        casa_container (Path): A path to a singularity container with CASA tooling.
        update_gain_cal_options (Optional[Dict[str, Any]], optional): Options used to overwrite the default ``gaincal`` options. Defaults to None.
        archive_input_ms (bool, optional): If True the input measurement set is zipped. Defaults to False.
        skip_selfcal (bool, optional): Should this self-cal be skipped. If `True`, the a new MS is created but not calibrated the appropriate new name and returned.
        rename_ms (bool, optional): It `True` simply rename a MS and adjust columns appropriately (potentially deleting them) instead of copying the complete MS. If `True` `archive_input_ms` is ignored. Defaults to False.
        archive_cal_table (bool, optional): Archive the output calibration table in a tarball. Defaults to False.

    Raises:
        ValueError: Raised when a ``.ms`` attribute can not be obtained

    Returns:
        MS: Self-calibrated measurement set
    """
    # TODO: Need to do a better type system to include the .ms
    # TODO: This needs to be expanded to handle multiple MS
    ms = ms if isinstance(ms, MS) else ms.ms  # type: ignore

    if not isinstance(ms, MS):
        raise ValueError(
            f"Unsupported {type(ms)=} {ms=}. Likely multiple MS instances? This is not yet supported. "
        )

    return gaincal_applycal_ms(
        ms=ms,
        round=selfcal_round,
        casa_container=casa_container,
        update_gain_cal_options=update_gain_cal_options,
        archive_input_ms=archive_input_ms,
        skip_selfcal=skip_selfcal,
        rename_ms=rename_ms,
        archive_cal_table=archive_cal_table,
    )


@task
def task_wsclean_imager(
    in_ms: ApplySolutions | MS,
    wsclean_container: Path,
    update_wsclean_options: dict[str, Any] | None = None,
    fits_mask: FITSMaskNames | None = None,
    channel_range: tuple[int, int] | None = None,
    make_cube_from_subbands: bool = True,
) -> WSCleanResult:
    """Run the wsclean imager against an input measurement set

    Args:
        in_ms (Union[ApplySolutions, MS]): The measurement set that will be imaged
        wsclean_container (Path): Path to a singularity container with wsclean packages
        update_wsclean_options (Optional[Dict[str, Any]], optional): Options to update from the default wsclean options. Defaults to None.
        fits_mask (Optional[FITSMaskNames], optional): A path to a clean guard mask. Defaults to None.
        channel_range (Optional[Tuple[int,int]], optional): Add to the wsclean options the specific channel range to be imaged. Defaults to None.

    Returns:
        WSCleanResult: A resulting wsclean command and resulting meta-data
    """
    from flint.exceptions import CleanDivergenceError

    ms = in_ms if isinstance(in_ms, MS) else in_ms.ms

    update_wsclean_options = (
        {} if update_wsclean_options is None else update_wsclean_options
    )

    if fits_mask:
        update_wsclean_options["fits_mask"] = fits_mask.mask_fits

    if channel_range:
        update_wsclean_options["channel_range"] = channel_range

    logger.info(f"wsclean inager {ms=}")
    try:
        return wsclean_imager(
            ms=ms,
            wsclean_container=wsclean_container,
            update_wsclean_options=update_wsclean_options,
            make_cube_from_subbands=make_cube_from_subbands,
        )
    except CleanDivergenceError:
        # NOTE: If the cleaning failed retry with some larger images
        # and slower cleaning. Perhaps this should be moved closer
        # to the wscleaning functionality
        size = (
            update_wsclean_options["size"] + 1024
            if "size" in update_wsclean_options
            else 8196
        )
        mgain = (
            max(0, update_wsclean_options["mgain"] - 0.1)
            if "mgain" in update_wsclean_options
            else 0.6
        )
        convergence_wsclean_options = dict(size=size, mgain=mgain)
        # dicts are mutable. Don't want to change for everything. Unclear to me
        # how prefect would behave here.
        update_wsclean_options = update_wsclean_options.copy()
        update_wsclean_options.update(**convergence_wsclean_options)
        logger.warning(
            f"Clean divergence dertected. Rerunning. Updated options {convergence_wsclean_options=}"
        )

        return wsclean_imager(
            ms=ms,
            wsclean_container=wsclean_container,
            update_wsclean_options=update_wsclean_options,
            make_cube_from_subbands=make_cube_from_subbands,
        )


def get_common_beam_from_images(
    image_paths: list[Path],
    cutoff: float = 25,
    filter_str: str | None = None,
    fixed_beam_shape: list[float] | None = None,
) -> BeamShape:
    """Compute a common beam size that all input images will be convoled to.

    Args:
        image_paths (list[Path]): Input images whose restoring beam properties will be considered
        cutoff (float, optional): Major axis larger than this valur, in arcseconds, will be ignored. Defaults to 25.
        filter_str (Optional[str], optional): Only include images when considering beam shape if this string is in the file path. Defaults to None.
        fixed_beam_shape (Optional[List[float]], optional): Specify the final beamsize of linmos field images in (arcsec, arcsec, deg). If None it is deduced from images. Defaults to None;

    Returns:
        BeamShape: The final convolving beam size to be used
    """
    # TODO: This function could have a wrapper around it that checks to see if
    # fixed_beam_shape is present, and simply return, avoiding using this functions
    # .submit method. Ahhh.
    if fixed_beam_shape:
        beam_shape = BeamShape(
            bmaj_arcsec=fixed_beam_shape[0],
            bmin_arcsec=fixed_beam_shape[1],
            bpa_deg=fixed_beam_shape[2],
        )
        logger.info(f"Using fixed {beam_shape=}")
        return beam_shape

    if filter_str:
        image_paths = [image for image in image_paths if filter_str in str(image)]

    logger.info(f"Considering {len(image_paths)} images. ")

    beam_shape = get_common_beam(image_paths=image_paths, cutoff=cutoff)
    if np.isnan(beam_shape.bmaj_arcsec):
        logger.critical("Failed to get beam resolution for:")
        logger.critical(f"{image_paths=}")
        logger.critical(f"{cutoff=}")
        logger.critical(f"{filter_str=}")

    return beam_shape


task_get_common_beam_from_images: Task[P, R] = task(get_common_beam_from_images)


def get_common_beam_from_image_set(
    image_set: ImageSet,
    cutoff: float = 25,
    filter_str: str | None = None,
    fixed_beam_shape: list[float] | None = None,
) -> BeamShape:
    """Compute a common beam size that all input images will be convoled to.

    Args:
        wsclean_results (Collection[WSCleanResult]): Input images whose restoring beam properties will be considered
        cutoff (float, optional): Major axis larger than this valur, in arcseconds, will be ignored. Defaults to 25.
        filter_str (Optional[str], optional): Only include images when considering beam shape if this string is in the file path. Defaults to None.
        fixed_beam_shape (Optional[List[float]], optional): Specify the final beamsize of linmos field images in (arcsec, arcsec, deg). If None it is deduced from images. Defaults to None;

    Returns:
        BeamShape: The final convolving beam size to be used
    """
    return get_common_beam_from_images(
        image_paths=image_set.image,
        cutoff=cutoff,
        filter_str=filter_str,
        fixed_beam_shape=fixed_beam_shape,
    )


task_get_common_beam_from_image_set: Task[P, R] = task(get_common_beam_from_image_set)


def get_common_beam_from_image_sets(
    image_sets: list[ImageSet],
    cutoff: float = 25,
    filter_str: str | None = None,
    fixed_beam_shape: list[float] | None = None,
) -> BeamShape:
    """Compute a common beam size that all input images will be convoled to.

    Args:
        wsclean_results (Collection[WSCleanResult]): Input images whose restoring beam properties will be considered
        cutoff (float, optional): Major axis larger than this valur, in arcseconds, will be ignored. Defaults to 25.
        filter_str (Optional[str], optional): Only include images when considering beam shape if this string is in the file path. Defaults to None.
        fixed_beam_shape (Optional[List[float]], optional): Specify the final beamsize of linmos field images in (arcsec, arcsec, deg). If None it is deduced from images. Defaults to None;

    Returns:
        BeamShape: The final convolving beam size to be used
    """
    images_to_consider: list[Path] = []
    for image_set in image_sets:
        images_to_consider.extend(image_set.image)
    return get_common_beam_from_images(
        image_paths=images_to_consider,
        cutoff=cutoff,
        filter_str=filter_str,
        fixed_beam_shape=fixed_beam_shape,
    )


task_get_common_beam_from_image_sets: Task[P, R] = task(get_common_beam_from_image_sets)


def get_common_beam_from_results(
    wsclean_results: list[WSCleanResult],
    cutoff: float = 25,
    filter_str: str | None = None,
    fixed_beam_shape: list[float] | None = None,
) -> BeamShape:
    """Compute a common beam size that all input images will be convoled to.

    Args:
        wsclean_results (Collection[WSCleanResult]): Input images whose restoring beam properties will be considered
        cutoff (float, optional): Major axis larger than this valur, in arcseconds, will be ignored. Defaults to 25.
        filter_str (Optional[str], optional): Only include images when considering beam shape if this string is in the file path. Defaults to None.
        fixed_beam_shape (Optional[List[float]], optional): Specify the final beamsize of linmos field images in (arcsec, arcsec, deg). If None it is deduced from images. Defaults to None;

    Returns:
        BeamShape: The final convolving beam size to be used
    """
    images_to_consider: list[Path] = []

    # TODO: This should support other image types
    for wsclean_result in wsclean_results:
        if wsclean_result.image_set is None:
            logger.warning(
                f"No image_set for {wsclean_result.ms} found. Has imager finished?"
            )
            continue
        images_to_consider.extend(wsclean_result.image_set.image)

    return get_common_beam_from_images(
        image_paths=images_to_consider,
        cutoff=cutoff,
        filter_str=filter_str,
        fixed_beam_shape=fixed_beam_shape,
    )


task_get_common_beam_from_results: Task[P, R] = task(get_common_beam_from_results)


@task
def task_get_cube_common_beam(
    wsclean_results: Collection[WSCleanResult],
    cutoff: float = 25,
) -> list[BeamShape]:
    """Compute a common beam size  for input cubes.

    Args:
        wsclean_results (Collection[WSCleanResult]): Input images whose restoring beam properties will be considered
        cutoff (float, optional): Major axis larger than this valur, in arcseconds, will be ignored. Defaults to 25.

    Returns:
        List[BeamShape]: The final convolving beam size to be used per channel in cubes
    """

    images_to_consider: list[Path] = []

    # TODO: This should support other image types
    for wsclean_result in wsclean_results:
        if wsclean_result.image_set is None:
            logger.warning(
                f"No image_set for {wsclean_result.ms} found. Has imager finished?"
            )
            continue
        images_to_consider.extend(wsclean_result.image_set.image)

    images_to_consider = get_fits_cube_from_paths(paths=images_to_consider)

    logger.info(
        f"Considering {len(images_to_consider)} images across {len(wsclean_results)} outputs. "
    )

    beam_shapes = get_cube_common_beam(cube_paths=images_to_consider, cutoff=cutoff)

    return beam_shapes


@task
def task_convolve_cube(
    wsclean_result: WSCleanResult,
    beam_shapes: list[BeamShape],
    cutoff: float = 60,
    mode: Literal["image"] = "image",
    convol_suffix_str: str = "conv",
) -> Collection[Path]:
    """Convolve images to a specified resolution

    Args:
        wsclean_result (WSCleanResult): Collection of output images from wsclean that will be convolved
        beam_shapes (BeamShape): The shape images will be convolved to
        cutoff (float, optional): Maximum major beam axis an image is allowed to have before it will not be convolved. Defaults to 60.
        convol_suffix_str (str, optional): The suffix added to the convolved images. Defaults to 'conv'.

    Returns:
        Collection[Path]: Path to the output images that have been convolved.
    """
    assert wsclean_result.image_set is not None, (
        f"{wsclean_result.ms} has no attached image_set."
    )

    supported_modes = ("image",)
    logger.info(f"Extracting {mode}")
    if mode == "image":
        image_paths = list(wsclean_result.image_set.image)
    else:
        raise ValueError(f"{mode=} is not supported. Known modes are {supported_modes}")

    logger.info(f"Extracting cubes from image_set {mode=}")
    image_paths = get_fits_cube_from_paths(paths=image_paths)

    # It is possible depending on how aggressively cleaning image products are deleted that these
    # some cleaning products (e.g. residuals) do not exist. There are a number of ways one could consider
    # handling this. The pirate in me feels like less is more, so an error will be enough. Keeping
    # things simple and avoiding the problem is probably the better way of dealing with this
    # situation. In time this would mean that we inspect and handle conflicting pipeline options.
    assert image_paths is not None, (
        f"{image_paths=} for {mode=} and {wsclean_result.image_set=}"
    )

    logger.info(f"Will convolve {image_paths}")

    return convolve_cubes(
        cube_paths=image_paths,
        beam_shapes=beam_shapes,
        cutoff=cutoff,
        convol_suffix=convol_suffix_str,
    )


def convolve_image_set(
    image_set: ImageSet,
    beam_shape: BeamShape,
    cutoff: float = 60,
    mode: str = "image",
    filter_str: str | None = None,
    convol_suffix_str: str = "conv",
    remove_original_images: bool = False,
) -> list[Path]:
    """Convolve images to a specified resolution

    Args:
        wsclean_result (WSCleanResult): Collection of output images from wsclean that will be convolved
        beam_shape (BeamShape): The shape images will be convolved to
        cutoff (float, optional): Maximum major beam axis an image is allowed to have before it will not be convolved. Defaults to 60.
        filter_str (Optional[str], optional): This string must be contained in the image path for it to be convolved. Defaults to None.
        convol_suffix_str (str, optional): The suffix added to the convolved images. Defaults to 'conv'.
        remove_original_images (bool, optional): If True remove the original image after they have been convolved. Defaults to False.

    Returns:
        Collection[Path]: Path to the output images that have been convolved.
    """
    supported_modes = ("image", "residual")
    logger.info(f"Extracting {mode}")
    if mode == "image":
        image_paths = list(image_set.image)
    elif mode == "residual":
        assert image_set.residual is not None, (
            f"{image_set.residual=}, which should not happen"
        )
        image_paths = list(image_set.residual)
    else:
        raise ValueError(f"{mode=} is not supported. Known modes are {supported_modes}")

    if filter_str is not None:
        logger.info(f"Filtering images paths with {filter_str=}")
        image_paths = [
            image_path for image_path in image_paths if filter_str in str(image_path)
        ]

    # It is possible depending on how aggressively cleaning image products are deleted that these
    # some cleaning products (e.g. residuals) do not exist. There are a number of ways one could consider
    # handling this. The pirate in me feels like less is more, so an error will be enough. Keeping
    # things simple and avoiding the problem is probably the better way of dealing with this
    # situation. In time this would mean that we inspect and handle conflicting pipeline options.
    assert image_paths is not None, f"{image_paths=} for {mode=} and {image_set=}"

    logger.info(f"Will convolve {image_paths}")

    # experience has shown that astropy units do not always work correctly
    # in a multiprocessing / dask environment. The unit registry does not
    # seem to serialise correctly, and we can get weird arcsecond is not
    # compatible with arcsecond type errors. Import here in an attempt
    # to minimise
    import astropy.units as u
    from astropy.io import fits
    from radio_beam import Beam

    # Print the beams out here for logging
    for image_path in image_paths:
        image_beam = Beam.from_fits_header(fits.getheader(str(image_path)))
        logger.info(
            f"{image_path.name!s}: {image_beam.major.to(u.arcsecond)} {image_beam.minor.to(u.arcsecond)}  {image_beam.pa}"
        )

    convolved_images = convolve_images(
        image_paths=image_paths,
        beam_shape=beam_shape,
        cutoff=cutoff,
        convol_suffix=convol_suffix_str,
    )

    if remove_original_images:
        logger.info(f"Removing {len(image_paths)} input images")
        _ = [image_path.unlink() for image_path in image_paths]  # type: ignore

    return convolved_images


task_convolve_image_set: Task[P, R] = task(convolve_image_set)


@task
def task_convolve_image(
    wsclean_result: WSCleanResult,
    beam_shape: BeamShape,
    cutoff: float = 60,
    mode: str = "image",
    filter_str: str | None = None,
    convol_suffix_str: str = "conv",
    remove_original_images: bool = False,
) -> Collection[Path]:
    """Convolve images to a specified resolution

    Args:
        wsclean_result (WSCleanResult): Collection of output images from wsclean that will be convolved
        beam_shape (BeamShape): The shape images will be convolved to
        cutoff (float, optional): Maximum major beam axis an image is allowed to have before it will not be convolved. Defaults to 60.
        filter_str (Optional[str], optional): This string must be contained in the image path for it to be convolved. Defaults to None.
        convol_suffix_str (str, optional): The suffix added to the convolved images. Defaults to 'conv'.
        remove_original_images (bool, optional): If True remove the original image after they have been convolved. Defaults to False.

    Returns:
        Collection[Path]: Path to the output images that have been convolved.
    """
    image_set = wsclean_result.image_set
    assert isinstance(image_set, ImageSet), (
        f"{image_set=}, but should be ImageSet instance"
    )
    return convolve_image_set(
        image_set=image_set,
        beam_shape=beam_shape,
        cutoff=cutoff,
        mode=mode,
        filter_str=filter_str,
        convol_suffix_str=convol_suffix_str,
        remove_original_images=remove_original_images,
    )


@task
def task_linmos_images(
    image_list: list[Path],
    container: Path,
    linmos_options: LinmosOptions,
    field_summary: FieldSummary | None = None,
    suffix_str: str | None = None,
    parset_output_path: str | None = None,
) -> LinmosResult:
    """Linmos together a set of input images.

    The ``linmos_options.image_output_name`` is updated to be
    a name based on the commonn unique fields of ``image_list``.

    Args:
        image_list (list[Path]): Collection of images to coadd
        container (Path): Path to a yandasoft singularity container
        linmos_options (LinmosOptions): Collection of linmos options
        field_summary (FieldSummary | None, optional): Description of the field, used to get the ``pol_axis`` of the field. Defaults to None.
        suffix_str (str | None, optional): Additional suffix str to add when generating the output file names. Defaults to None.
        parset_output_path (str | None, optional): The output parameter set that will be generated. Defaults to None.

    Returns:
        LinmosResult: Collection of linmos items generated
    """
    # TODO: Need a better filter_str approach. Would probably be better to
    # have literals for the type of product (MFS, cube, model) to be
    # sure of appropriate extraction

    from flint.naming import create_linmos_base_path
    from flint.utils import flatten_items

    image_list = flatten_items(items=image_list)

    output_path = create_linmos_base_path(
        input_images=image_list,
        additional_suffixes=suffix_str,
    )

    linmos_options = linmos_options.with_options(
        base_output_name=output_path,
        pol_axis=field_summary.pol_axis if field_summary else None,
    )

    linmos_result = linmos_images(
        images=image_list,
        linmos_options=linmos_options,
        container=container,
    )

    return linmos_result


def convolve_then_linmos(
    wsclean_results: Collection[WSCleanResult],
    beam_shape: BeamShape,
    field_options: FieldOptions | SubtractFieldOptions,
    linmos_suffix_str: str | None,
    field_summary: FieldSummary | None = None,
    convol_mode: str = "image",
    convol_filter: str = ".MFS.",
    convol_suffix_str: str = "conv",
    trim_linmos_fits: bool = True,
    remove_original_images: bool = False,
    cleanup_linmos: bool = False,
) -> LinmosResult:
    """An internal function that launches the convolution to a common resolution
    and subsequent linmos of the wsclean residual images.

    Args:
        wsclean_results (Collection[WSCleanResult]): Collection of wsclean imaging results, with residual images described in the attached ``ImageSet``
        beam_shape (BeamShape): The beam shape that residual images will be convolved to
        field_options (FieldOptions): Options related to the processing of the field
        linmos_suffix_str (str): The suffix string passed to the linmos parset name
        field_summary (Optional[FieldSummary], optional): The summary of the field, including (importantly) to orientation of the third-axis. Defaults to None.
        convol_mode (str, optional): The mode passed to the convol task to describe the images to extract. Support image or residual.  Defaults to image.
        convol_filter (str, optional): A text file applied when assessing images to co-add. Defaults to '.MFS.'.
        convol_suffix_str (str, optional): The suffix added to the convolved images. Defaults to 'conv'.
        trim_linmos_fits (bool, optional): Attempt to trim the output linmos files of as much empty space as possible. Defaults to True.
        remove_original_images (bool, optional): If True remove the original image after they have been convolved. Defaults to False.
        cleanup_linmos (bool, optional): Clean up items created throughout linmos, including the per-channel weight text files for each input image. Defaults to False.

    Returns:
        LinmosResult: Resulting linmos command parset
    """

    conv_images = task_convolve_image.map(
        wsclean_result=wsclean_results,
        beam_shape=unmapped(beam_shape),  # type: ignore
        cutoff=field_options.beam_cutoff,
        mode=convol_mode,
        filter_str=convol_filter,
        convol_suffix_str=convol_suffix_str,
        remove_original_images=remove_original_images,
    )
    assert field_options.yandasoft_container is not None
    # Though pol axis could be obtained from field_summary, at this point
    # it could be a PrefectFuture. Pass it over to the task.
    linmos_options = LinmosOptions(
        holofile=field_options.holofile,
        cutoff=field_options.pb_cutoff,
        trim_linmos_fits=trim_linmos_fits,
        cleanup=cleanup_linmos,
        remove_original_images=remove_original_images,
    )
    parset = task_linmos_images.submit(
        image_list=flatten_items(items=conv_images),
        container=field_options.yandasoft_container,
        suffix_str=linmos_suffix_str,
        linmos_options=linmos_options,
        field_summary=field_summary,
    )  # type: ignore

    return parset


def create_convol_linmos_images(
    wsclean_results: Collection[WSCleanResult],
    field_options: FieldOptions,
    field_summary: FieldSummary | None = None,
    additional_linmos_suffix_str: str | None = None,
) -> list[LinmosResult]:
    """Derive the appropriate set of beam shapes and then produce corresponding
    convolved and co-added images

    Args:
        wsclean_results (Collection[WSCleanResult]): Set of wsclean commands that have been executed
        field_options (FieldOptions): Set of field imaging options, containing details of the beam/s
        field_summary (Optional[FieldSummary], optional): Summary of the MSs, importantly containing their third-axis rotation. Defaults to None.
        additional_linmos_suffix_str (Optional[str], optional): An additional string added to the end of the auto-generated linmos base name. Defaults to None.

    Returns:
        List[LinmosResult]: The collection of linmos commands executed.
    """
    parsets: list[LinmosResult] = []

    beam_str: str = get_beam_resolution_str(mode="optimal")
    linmos_suffixes: list[str] = [beam_str]
    if additional_linmos_suffix_str:
        linmos_suffixes.insert(0, additional_linmos_suffix_str)

    # set up the expected name formats
    linmos_suffix_str = ".".join(linmos_suffixes)
    convol_suffix_str = f"{beam_str}.conv"

    beam_shape = task_get_common_beam_from_results.submit(
        wsclean_results=wsclean_results,
        cutoff=field_options.beam_cutoff,
        filter_str=".MFS.",
    )
    # NOTE: The order matters here. The last linmos file is used
    # when running the source finding. Putting this order around means
    # we would source find on the residual image
    if field_options.linmos_residuals:
        parsets.append(
            convolve_then_linmos(
                wsclean_results=wsclean_results,
                beam_shape=beam_shape,  # type: ignore
                field_options=field_options,
                linmos_suffix_str=f"{linmos_suffix_str}.residual",
                field_summary=field_summary,
                convol_mode="residual",
                convol_filter=".MFS.",
                convol_suffix_str=convol_suffix_str,
            )
        )
    parsets.append(
        convolve_then_linmos(
            wsclean_results=wsclean_results,
            beam_shape=beam_shape,  # type: ignore
            field_options=field_options,
            linmos_suffix_str=linmos_suffix_str,
            field_summary=field_summary,
            convol_mode="image",
            convol_filter=".MFS.",
            convol_suffix_str=convol_suffix_str,
        )
    )

    return parsets


@task
def task_convolve_linmos_to_fixed_shape(
    linmos_result: LinmosResult, field_options: FieldOptions
) -> LinmosResult:
    """Smooth the linmos input image to a fixed resolution

    Args:
        linmos_result (LinmosResult): An existing linmos results set representing a created image
        field_options (FieldOptions): A field options instance with an set fixed beeam shape set

    Returns:
        LinmosResult: A smoothed version of the input linmos image
    """
    from flint.naming import update_beam_resolution_field_in_path

    image_to_smooth = linmos_result.image_fits

    output_image_path: Path = update_beam_resolution_field_in_path(
        path=image_to_smooth, original_mode="optimal", updated_mode="fixed", marker="."
    )

    assert field_options.fixed_beam_shape, (
        f"{field_options.fixed_beam_shape=}, which is not allowed"
    )
    beam_shape = BeamShape(
        bmaj_arcsec=field_options.fixed_beam_shape[0],
        bmin_arcsec=field_options.fixed_beam_shape[1],
        bpa_deg=field_options.fixed_beam_shape[2],
    )

    # There is no option to provide a fully qualified name. So, capture then rename
    # the smoothed file
    images_to_smooth = [image_to_smooth]
    smoothed_linmos_paths = convolve_images(
        image_paths=images_to_smooth,
        beam_shape=beam_shape,
        convol_suffix="conv_to_rename",
        output_paths=[output_image_path],
    )
    assert len(smoothed_linmos_paths) == len(images_to_smooth) == 1, (
        f"Need matching path lengths between {images_to_smooth=} and {smoothed_linmos_paths=}"
    )

    return linmos_result.with_options(image_fits=output_image_path)


def create_convolve_linmos_cubes(
    wsclean_results: Collection[WSCleanResult],
    field_options: FieldOptions,
    current_round: int | None = None,
    additional_linmos_suffix_str: str | None = "cube",
):
    suffixes = [f"round{current_round}" if current_round is not None else "noselfcal"]
    if additional_linmos_suffix_str:
        suffixes.insert(0, additional_linmos_suffix_str)
    linmos_suffix_str = ".".join(suffixes)

    beam_shapes = task_get_cube_common_beam.submit(
        wsclean_results=wsclean_results, cutoff=field_options.beam_cutoff
    )
    convolved_cubes = task_convolve_cube.map(
        wsclean_result=wsclean_results,  # type: ignore
        cutoff=field_options.beam_cutoff,
        mode=unmapped("image"),  # type: ignore
        beam_shapes=unmapped(beam_shapes),  # type: ignore
    )

    assert field_options.yandasoft_container is not None
    linmos_options = LinmosOptions(
        holofile=field_options.holofile,
        cutoff=field_options.pb_cutoff,
    )
    parset = task_linmos_images.submit(
        image_list=convolved_cubes,  # type: ignore
        container=field_options.yandasoft_container,
        suffix_str=linmos_suffix_str,
        linmos_options=linmos_options,
    )
    return parset


@task
def task_create_image_mask_model(
    image: LinmosResult | ImageSet | WSCleanResult,
    image_products: AegeanOutputs,
    update_masking_options: dict[str, Any] | None = None,
) -> FITSMaskNames:
    """Create a mask for an image, with the intention of providing it as a clean mask
    to an appropriate imager.

    Args:
        linmos_parset (LinmosResult): Linmos command and associated meta-data
        image_products (AegeanOutputs): Images of the RMS and BKG
        update_masking_options (Optional[Dict[str,Any]], optional): Updated options supplied to the default MaskingOptions. Defaults to None.


    Raises:
        ValueError: Raised when ``image_products`` are not known

    Returns:
        FITSMaskNames: Clean mask where all pixels below a S/N are masked
    """
    if isinstance(image_products, AegeanOutputs):
        source_rms = image_products.rms
        source_bkg = image_products.bkg
    else:
        raise ValueError("Unsupported bkg/rms mode. ")

    source_image = None
    if isinstance(image, LinmosResult):
        source_image = image.image_fits
    elif isinstance(image, ImageSet) and image.image is not None:
        source_image = list(image.image)[-1]
    elif isinstance(image, WSCleanResult) and image.image_set is not None:
        source_image = list(image.image_set.image)[-1]
    else:
        source_image = image_products.image

    if source_image is None:
        raise ValueError(f"Unsupported image mode. Received {type(image)} ")

    logger.info(f"Creating a clean mask for {source_image=}")
    logger.info(f"Using {source_rms=}")
    logger.info(f"Using {source_bkg=}")

    masking_options = MaskingOptions()
    if update_masking_options:
        masking_options = masking_options.with_options(**update_masking_options)

    mask_names = create_snr_mask_from_fits(
        fits_image_path=source_image,
        fits_bkg_path=source_bkg,
        fits_rms_path=source_rms,
        masking_options=masking_options,
        create_signal_fits=True,
    )

    logger.info(f"Created {mask_names.mask_fits}")

    return mask_names


@task
def task_extract_beam_mask_image(
    linmos_mask_names: FITSMaskNames, wsclean_result: WSCleanResult
) -> FITSMaskNames:
    """Extract a clean mask for a beam from a larger clean mask (e.g. derived from a field)

    Args:
        linmos_mask_names (FITSMaskNames): Mask that will be drawn from to form a smaller clean mask (e.g. for a beam)
        wsclean_result (WSCleanResult): Wsclean command and meta-data. This is used to draw from the WCS to create an appropriate pixel-to-pixel mask

    Returns:
        FITSMaskNames: Clean mask for a image
    """
    # All images made by wsclean will have the same WCS
    assert wsclean_result.image_set is not None, (
        f"{wsclean_result.image_set=}, which should not happen"
    )
    beam_image = next(iter(wsclean_result.image_set.image))
    beam_mask_names = extract_beam_mask_from_mosaic(
        fits_beam_image_path=beam_image, fits_mosaic_mask_names=linmos_mask_names
    )

    return beam_mask_names


@task
def task_create_validation_plot(
    field_summary: FieldSummary,
    aegean_outputs: AegeanOutputs,
    reference_catalogue_directory: Path,
    upload_artifact: bool = True,
) -> Path:
    """Create a multi-panel figure highlighting the RMS, flux scale and astrometry of a field

    Args:
        aegean_outputs (AegeanOutputs): Output aegean products
        reference_catalogue_directory (Path): Directory containing NVSS, SUMSS and ICRS reference catalogues. These catalogues are reconginised internally and have expected names.
        upload_artifact (bool, optional): If True the validation plot will be uploaded to the prefect service as an artifact. Defaults to True.

    Returns:
        Path: Path to the output figure created
    """
    output_path = aegean_outputs.comp.parent

    logger.info(f"Will create validation plot in {output_path=}")

    plot_path = create_validation_plot(
        field_summary=field_summary,
        rms_image_path=aegean_outputs.rms,
        source_catalogue_path=aegean_outputs.comp,
        output_path=output_path,
        reference_catalogue_directory=reference_catalogue_directory,
    )

    if upload_artifact:
        upload_image_as_artifact(
            image_path=plot_path, description=f"Validation plot {plot_path!s}"
        )

    return plot_path


@task
def task_create_validation_tables(
    field_summary: FieldSummary,
    aegean_outputs: AegeanOutputs,
    reference_catalogue_directory: Path,
    upload_artifacts: bool = True,
) -> ValidationTables:
    """Create a set of validation tables that can be used to assess the
    correctness of an image and associated source catalogue.

    Args:
        processed_ms_paths (List[Path]): The processed MS files that were used to create the source catalogue
        rms_image_path (Path): The RMS fits image the source catalogue was constructed against.
        source_catalogue_path (Path): The source catalogue.
        output_path (Path): The output path of the figure to create
        reference_catalogue_directory (Path): The directory that contains the reference catalogues installed

    Returns:
        ValidationTables: The tables that were created
    """
    output_path = aegean_outputs.comp.parent

    logger.info(f"Will create validation tables in {output_path=}")
    validation_tables = create_validation_tables(
        field_summary=field_summary,
        rms_image_path=aegean_outputs.rms,
        source_catalogue_path=aegean_outputs.comp,
        output_path=output_path,
        reference_catalogue_directory=reference_catalogue_directory,
    )

    if upload_artifacts:
        for table in validation_tables:
            if isinstance(table, Path):
                validation_df = pd.read_csv(table)
                df_dict = validation_df.to_dict("records")
                create_table_artifact(
                    table=df_dict,
                    description=f"{table.stem}",
                )  # type: ignore
            elif isinstance(table, XMatchTables):
                for subtable in table:
                    if subtable is None:
                        continue
                    if not isinstance(subtable, Path):
                        continue
                    sub_df = pd.read_csv(subtable)
                    df_dict = sub_df.to_dict("records")
                    create_table_artifact(
                        table=df_dict,
                        description=f"{subtable.stem}",
                    )  # type: ignore

    return validation_tables


def validation_items(
    field_summary: FieldSummary,
    aegean_outputs: AegeanOutputs,
    reference_catalogue_directory: Path,
):
    """Construct the validation plot and validation table items for the imaged field.

    Internally these are submitting the prefect task versions of:
    - `task_create_validation_plot`
    - `task_create_validation_tables`

    Args:
        field_summary (FieldSummary): Container representing the SBID being imaged and its populated characteristics
        aegean_outputs (AegeanOutputs): Source finding results
        reference_catalogue_directory (Path): Location of directory containing the reference known NVSS, SUMSS and ICRS catalogues
    """

    val_plot_path = task_create_validation_plot.submit(
        field_summary=field_summary,
        aegean_outputs=aegean_outputs,
        reference_catalogue_directory=reference_catalogue_directory,
    )
    validate_tables = task_create_validation_tables.submit(
        field_summary=field_summary,
        aegean_outputs=aegean_outputs,
        reference_catalogue_directory=reference_catalogue_directory,
    )

    return (val_plot_path, validate_tables)
