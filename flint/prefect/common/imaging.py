"""This contains common utilities to enable components of the prefect imaging flowws.
The majority of the items here are the task decorated functions. Effort should be made
to avoid putting in too many items that are not going to be directly used by prefect
imaging flows.
"""

from pathlib import Path
from typing import Any, Collection, Dict, List, Literal, Optional, TypeVar, Union, Tuple

import pandas as pd
from prefect import task, unmapped
from prefect.artifacts import create_table_artifact

from flint.calibrate.aocalibrate import (
    ApplySolutions,
    CalibrateCommand,
    create_apply_solutions_cmd,
    select_aosolution_for_ms,
)
from flint.coadd.linmos import LinmosCommand, linmos_images
from flint.configuration import wrapper_options_from_strategy
from flint.convol import (
    BeamShape,
    convolve_images,
    get_common_beam,
    get_cube_common_beam,
    convolve_cubes,
)
from flint.flagging import flag_ms_aoflagger
from flint.imager.wsclean import (
    ImageSet,
    WSCleanCommand,
    WSCleanOptions,
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
    processed_ms_format,
)
from flint.options import FieldOptions
from flint.peel.potato import potato_peel
from flint.prefect.common.utils import upload_image_as_artifact
from flint.selfcal.casa import gaincal_applycal_ms
from flint.source_finding.aegean import AegeanOutputs, run_bane_and_aegean
from flint.summary import FieldSummary
from flint.utils import zip_folder
from flint.validation import (
    ValidationTables,
    XMatchTables,
    create_validation_plot,
    create_validation_tables,
)

# These are simple task wrapped functions and require no other modification
task_copy_and_preprocess_casda_askap_ms = task(copy_and_preprocess_casda_askap_ms)
task_preprocess_askap_ms = task(preprocess_askap_ms)
task_split_by_field = task(split_by_field)
task_select_solution_for_ms = task(select_aosolution_for_ms)
task_create_apply_solutions_cmd = task(create_apply_solutions_cmd)
task_rename_column_in_ms = task(rename_column_in_ms)

# Tasks below are extracting componented from earlier stages, or are
# otherwise doing something important

FlagMS = TypeVar("FlagMS", MS, ApplySolutions)


@task
def task_potato_peel(
    ms: MS,
    potato_container: Path,
    update_potato_config_options: Optional[Dict[str, Any]] = None,
    update_potato_peel_options: Optional[Dict[str, Any]] = None,
    update_wsclean_options: Optional[Dict[str, Any]] = None,
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


# BANE sometimes gets cauht in some stalled staTE
@task(timeout_seconds=60 * 45, retries=3)
def task_run_bane_and_aegean(
    image: Union[WSCleanCommand, LinmosCommand], aegean_container: Path
) -> AegeanOutputs:
    """Run BANE and Aegean against a FITS image

    Args:
        image (Union[WSCleanCommand, LinmosCommand]): The image that will be searched
        aegean_container (Path): Path to a singularity container containing BANE and aegean

    Raises:
        ValueError: Raised when ``image`` is not a supported type

    Returns:
        AegeanOutputs: Output BANE and aegean products, including the RMS and BKG images
    """
    if isinstance(image, WSCleanCommand):
        assert image.imageset is not None, "Image set attribute unset. "
        image_paths = image.imageset.image

        logger.info(f"Have extracted image: {image_paths}")

        # For the moment, will only source find on an MFS image
        image_paths = [image for image in image_paths if ".MFS." in str(image)]
        assert (
            len(image_paths) == 1
        ), "More than one image found after filter for MFS only images. "
        # Get out the only path in the list.
        image_path = image_paths[0]
    elif isinstance(image, LinmosCommand):
        logger.info("Will be running aegean on a linmos image")

        image_path = image.image_fits
        assert image_path.exists(), f"Image path {image_path} does not exist"
    else:
        raise ValueError(f"Unexpected type, have received {type(image)} for {image=}. ")

    aegean_outputs = run_bane_and_aegean(
        image=image_path, aegean_container=aegean_container
    )

    return aegean_outputs


@task
def task_zip_ms(in_item: WSCleanCommand) -> Path:
    """Zip a measurement set

    Args:
        in_item (WSCleanCommand): The inpute item with a ``.ms`` attribute of type ``MS``.

    Returns:
        Path: Output path of the zipped measurement set
    """
    # TODO: This typing needs to be expanded
    ms = in_item.ms

    zipped_ms = zip_folder(in_path=ms.path)

    return zipped_ms


@task
@wrapper_options_from_strategy(update_options_keyword="update_gain_cal_options")
def task_gaincal_applycal_ms(
    ms: Union[MS, WSCleanCommand],
    selfcal_round: int,
    casa_container: Path,
    update_gain_cal_options: Optional[Dict[str, Any]] = None,
    archive_input_ms: bool = False,
    skip_selfcal: bool = False,
    rename_ms: bool = False,
    archive_cal_table: bool = False,
) -> MS:
    """Perform self-calibration using CASA gaincal and applycal.

    Args:
        ms (Union[MS, WSCleanCommand]): A resulting wsclean output. This is used purely to extract the ``.ms`` attribute.
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
@wrapper_options_from_strategy(update_options_keyword="update_wsclean_options")
def task_wsclean_imager(
    in_ms: Union[ApplySolutions, MS],
    wsclean_container: Path,
    update_wsclean_options: Optional[Dict[str, Any]] = None,
    fits_mask: Optional[FITSMaskNames] = None,
) -> WSCleanCommand:
    """Run the wsclean imager against an input measurement set

    Args:
        in_ms (Union[ApplySolutions, MS]): The measurement set that will be imaged
        wsclean_container (Path): Path to a singularity container with wsclean packages
        update_wsclean_options (Optional[Dict[str, Any]], optional): Options to update from the default wsclean options. Defaults to None.
        fits_mask (Optional[FITSMaskNames], optional): A path to a clean guard mask. Defaults to None.

    Returns:
        WSCleanCommand: A resulting wsclean command and resulting meta-data
    """
    from flint.exceptions import CleanDivergenceError

    ms = in_ms if isinstance(in_ms, MS) else in_ms.ms

    update_wsclean_options = (
        {} if update_wsclean_options is None else update_wsclean_options
    )

    if fits_mask:
        update_wsclean_options["fits_mask"] = fits_mask.mask_fits

    logger.info(f"wsclean inager {ms=}")
    try:
        return wsclean_imager(
            ms=ms,
            wsclean_container=wsclean_container,
            update_wsclean_options=update_wsclean_options,
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
        logger.warn(
            f"Clean divergence dertected. Rerunning. Updated options {convergence_wsclean_options=}"
        )

        return wsclean_imager(
            ms=ms,
            wsclean_container=wsclean_container,
            update_wsclean_options=update_wsclean_options,
        )


@task
def task_get_common_beam(
    wsclean_cmds: Collection[WSCleanCommand],
    cutoff: float = 25,
    filter: Optional[str] = None,
    fixed_beam_shape: Optional[List[float]] = None,
) -> BeamShape:
    """Compute a common beam size that all input images will be convoled to.

    Args:
        wsclean_cmds (Collection[WSCleanCommand]): Input images whose restoring beam properties will be considered
        cutoff (float, optional): Major axis larger than this valur, in arcseconds, will be ignored. Defaults to 25.
        filter (Optional[str], optional): Only include images when considering beam shape if this string is in the file path. Defaults to None.
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

    images_to_consider: List[Path] = []

    # TODO: This should support other image types
    for wsclean_cmd in wsclean_cmds:
        if wsclean_cmd.imageset is None:
            logger.warning(
                f"No imageset for {wsclean_cmd.ms} found. Has imager finished?"
            )
            continue
        images_to_consider.extend(wsclean_cmd.imageset.image)

    if filter:
        images_to_consider = [
            image for image in images_to_consider if filter in str(image)
        ]

    logger.info(
        f"Considering {len(images_to_consider)} images across {len(wsclean_cmds)} outputs. "
    )

    beam_shape = get_common_beam(image_paths=images_to_consider, cutoff=cutoff)

    return beam_shape


@task
def task_get_cube_common_beam(
    wsclean_cmds: Collection[WSCleanCommand],
    cutoff: float = 25,
) -> List[BeamShape]:
    """Compute a common beam size  for input cubes.

    Args:
        wsclean_cmds (Collection[WSCleanCommand]): Input images whose restoring beam properties will be considered
        cutoff (float, optional): Major axis larger than this valur, in arcseconds, will be ignored. Defaults to 25.

    Returns:
        List[BeamShape]: The final convolving beam size to be used per channel in cubes
    """

    images_to_consider: List[Path] = []

    # TODO: This should support other image types
    for wsclean_cmd in wsclean_cmds:
        if wsclean_cmd.imageset is None:
            logger.warning(
                f"No imageset for {wsclean_cmd.ms} found. Has imager finished?"
            )
            continue
        images_to_consider.extend(wsclean_cmd.imageset.image)

    images_to_consider = get_fits_cube_from_paths(paths=images_to_consider)

    logger.info(
        f"Considering {len(images_to_consider)} images across {len(wsclean_cmds)} outputs. "
    )

    beam_shapes = get_cube_common_beam(cube_paths=images_to_consider, cutoff=cutoff)

    return beam_shapes


@task
def task_convolve_cube(
    wsclean_cmd: WSCleanCommand,
    beam_shapes: List[BeamShape],
    cutoff: float = 60,
    mode: Literal["image"] = "image",
    convol_suffix_str: str = "conv",
) -> Collection[Path]:
    """Convolve images to a specified resolution

    Args:
        wsclean_cmd (WSCleanCommand): Collection of output images from wsclean that will be convolved
        beam_shapes (BeamShape): The shape images will be convolved to
        cutoff (float, optional): Maximum major beam axis an image is allowed to have before it will not be convolved. Defaults to 60.
        convol_suffix_str (str, optional): The suffix added to the convolved images. Defaults to 'conv'.

    Returns:
        Collection[Path]: Path to the output images that have been convolved.
    """
    assert (
        wsclean_cmd.imageset is not None
    ), f"{wsclean_cmd.ms} has no attached imageset."

    supported_modes = ("image",)
    logger.info(f"Extracting {mode}")
    if mode == "image":
        image_paths = list(wsclean_cmd.imageset.image)
    else:
        raise ValueError(f"{mode=} is not supported. Known modes are {supported_modes}")

    logger.info(f"Extracting cubes from imageset {mode=}")
    image_paths = get_fits_cube_from_paths(paths=image_paths)

    # It is possible depending on how aggressively cleaning image products are deleted that these
    # some cleaning products (e.g. residuals) do not exist. There are a number of ways one could consider
    # handling this. The pirate in me feels like less is more, so an error will be enough. Keeping
    # things simple and avoiding the problem is probably the better way of dealing with this
    # situation. In time this would mean that we inspect and handle conflicting pipeline options.
    assert (
        image_paths is not None
    ), f"{image_paths=} for {mode=} and {wsclean_cmd.imageset=}"

    logger.info(f"Will convolve {image_paths}")

    return convolve_cubes(
        cube_paths=image_paths,
        beam_shapes=beam_shapes,
        cutoff=cutoff,
        convol_suffix=convol_suffix_str,
    )


@task
def task_convolve_image(
    wsclean_cmd: WSCleanCommand,
    beam_shape: BeamShape,
    cutoff: float = 60,
    mode: str = "image",
    filter: Optional[str] = None,
    convol_suffix_str: str = "conv",
) -> Collection[Path]:
    """Convolve images to a specified resolution

    Args:
        wsclean_cmd (WSCleanCommand): Collection of output images from wsclean that will be convolved
        beam_shape (BeamShape): The shape images will be convolved to
        cutoff (float, optional): Maximum major beam axis an image is allowed to have before it will not be convolved. Defaults to 60.
        filter (Optional[str], optional): This string must be contained in the image path for it to be convolved. Defaults to None.
        convol_suffix_str (str, optional): The suffix added to the convolved images. Defaults to 'conv'.

    Returns:
        Collection[Path]: Path to the output images that have been convolved.
    """
    assert (
        wsclean_cmd.imageset is not None
    ), f"{wsclean_cmd.ms} has no attached imageset."

    supported_modes = ("image", "residual")
    logger.info(f"Extracting {mode}")
    if mode == "image":
        image_paths = list(wsclean_cmd.imageset.image)
    elif mode == "residual":
        assert (
            wsclean_cmd.imageset.residual is not None
        ), f"{wsclean_cmd.imageset.residual=}, which should not happen"
        image_paths = list(wsclean_cmd.imageset.residual)
    else:
        raise ValueError(f"{mode=} is not supported. Known modes are {supported_modes}")

    if filter:
        logger.info(f"Filtering images paths with {filter=}")
        image_paths = [
            image_path for image_path in image_paths if filter in str(image_path)
        ]

    # It is possible depending on how aggressively cleaning image products are deleted that these
    # some cleaning products (e.g. residuals) do not exist. There are a number of ways one could consider
    # handling this. The pirate in me feels like less is more, so an error will be enough. Keeping
    # things simple and avoiding the problem is probably the better way of dealing with this
    # situation. In time this would mean that we inspect and handle conflicting pipeline options.
    assert (
        image_paths is not None
    ), f"{image_paths=} for {mode=} and {wsclean_cmd.imageset=}"

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
            f"{str(image_path.name)}: {image_beam.major.to(u.arcsecond)} {image_beam.minor.to(u.arcsecond)}  {image_beam.pa}"
        )

    return convolve_images(
        image_paths=image_paths,
        beam_shape=beam_shape,
        cutoff=cutoff,
        convol_suffix=convol_suffix_str,
    )


@task
def task_linmos_images(
    images: Collection[Collection[Path]],
    container: Path,
    filter: Optional[str] = ".MFS.",
    field_name: Optional[str] = None,
    suffix_str: str = "noselfcal",
    holofile: Optional[Path] = None,
    sbid: Optional[Union[int, str]] = None,
    parset_output_path: Optional[str] = None,
    cutoff: float = 0.05,
    field_summary: Optional[FieldSummary] = None,
) -> LinmosCommand:
    """Run the yandasoft linmos task against a set of input images

    Args:
        images (Collection[Collection[Path]]): Images that will be co-added together
        container (Path): Path to singularity container that contains yandasoft
        filter (Optional[str], optional): Filter to extract the images that will be extracted from the set of input images. These will be co-added. If None all images are co-aded. Defaults to ".MFS.".
        field_name (Optional[str], optional): Name of the field, which is included in the output images created. Defaults to None.
        suffix_str (str, optional): Additional string added to the prefix of the output linmos image products. Defaults to "noselfcal".
        holofile (Optional[Path], optional): The FITS cube with the beam corrections derived from ASKAP holography. Defaults to None.
        sbid (Optional[Union[int,str]], optional): SBID of the data being imaged. Defaults to None.
        parset_output_path (Optional[str], optional): Location to write the linmos parset file to. Defaults to None.
        cutoff (float, optional): The primary beam attenuation cutoff supplied to linmos when coadding. Defaults to 0.05.
        field_summary (Optional[FieldSummary], optional): The summary of the field, including (importantly) to orientation of the third-axis. Defaults to None.

    Returns:
        LinmosCommand: The linmos command and associated meta-data
    """
    # TODO: Need to flatten images
    # TODO: Need a better way of getting field names

    # TODO: Need a better filter approach. Would probably be better to
    # have literals for the type of product (MFS, cube, model) to be
    # sure of appropriate extraction
    all_images = [img for beam_images in images for img in beam_images]
    logger.info(f"Number of images to examine {len(all_images)}")

    filter_images = (
        [img for img in all_images if filter in str(img)] if filter else all_images
    )
    logger.info(f"Number of filtered images to linmos: {len(filter_images)}")

    candidate_image = filter_images[0]
    candidate_image_fields = processed_ms_format(in_name=candidate_image)
    assert (
        candidate_image_fields is not None
    ), f"{candidate_image=}, which should not happen"

    if field_name is None:
        field_name = candidate_image_fields.field
        logger.info(f"Extracted {field_name=} from {candidate_image=}")

    if sbid is None:
        sbid = candidate_image_fields.sbid
        logger.info(f"Extracted {sbid=} from {candidate_image=}")

    base_name = f"SB{sbid}.{field_name}.{suffix_str}"

    out_dir = Path(filter_images[0].parent)
    out_name = out_dir / base_name
    logger.info(f"Base output image name will be: {out_name}")

    out_file_name = (
        parset_output_path
        if parset_output_path
        else Path(f"{out_name.name}_parset.txt")
    )

    assert out_dir is not None, f"{out_dir=}, which should not happen"
    output_path: Path = Path(out_dir) / Path(out_file_name)  # type: ignore
    logger.info(f"Parsert output path is {parset_output_path}")

    pol_axis = field_summary.pol_axis if field_summary else None

    linmos_cmd = linmos_images(
        images=filter_images,
        parset_output_path=Path(output_path),
        image_output_name=str(out_name),
        container=container,
        holofile=holofile,
        cutoff=cutoff,
        pol_axis=pol_axis,
    )

    return linmos_cmd


def _convolve_linmos(
    wsclean_cmds: Collection[WSCleanCommand],
    beam_shape: BeamShape,
    field_options: FieldOptions,
    linmos_suffix_str: str,
    field_summary: Optional[FieldSummary] = None,
    convol_mode: str = "image",
    convol_filter: str = ".MFS.",
    convol_suffix_str: str = "conv",
) -> LinmosCommand:
    """An internal function that launches the convolution to a common resolution
    and subsequent linmos of the wsclean residual images.

    Args:
        wsclean_cmds (Collection[WSCleanCommand]): Collection of wsclean imaging results, with residual images described in the attached ``ImageSet``
        beam_shape (BeamShape): The beam shape that residual images will be convolved to
        field_options (FieldOptions): Options related to the processing of the field
        linmos_suffix_str (str): The suffix string passed to the linmos parset name
        field_summary (Optional[FieldSummary], optional): The summary of the field, including (importantly) to orientation of the third-axis. Defaults to None.
        convol_mode (str, optional): The mode passed to the convol task to describe the images to extract. Support image or residual.  Defaults to image.
        convol_filter (str, optional): A text file applied when assessing images to co-add. Defaults to '.MFS.'.
        convol_suffix_str (str, optional): The suffix added to the convolved images. Defaults to 'conv'.

    Returns:
        LinmosCommand: Resulting linmos command parset
    """

    conv_images = task_convolve_image.map(
        wsclean_cmd=wsclean_cmds,
        beam_shape=unmapped(beam_shape),  # type: ignore
        cutoff=field_options.beam_cutoff,
        mode=convol_mode,
        filter=convol_filter,
        convol_suffix_str=convol_suffix_str,
    )
    assert field_options.yandasoft_container is not None
    parset = task_linmos_images.submit(
        images=conv_images,  # type: ignore
        container=field_options.yandasoft_container,
        suffix_str=linmos_suffix_str,
        holofile=field_options.holofile,
        cutoff=field_options.pb_cutoff,
        field_summary=field_summary,
    )  # type: ignore

    return parset


def _create_convol_linmos_images(
    wsclean_cmds: Collection[WSCleanCommand],
    field_options: FieldOptions,
    field_summary: Optional[FieldSummary] = None,
    current_round: Optional[int] = None,
    additional_linmos_suffix_str: Optional[str] = None,
) -> List[LinmosCommand]:
    """Derive the appropriate set of beam shapes and then produce corresponding
    convolved and co-added images

    Args:
        wsclean_cmds (Collection[WSCleanCommand]): Set of wsclean commands that have been executed
        field_options (FieldOptions): Set of field imaging options, containing details of the beam/s
        field_summary (Optional[FieldSummary], optional): Summary of the MSs, importantly containing their third-axis rotation. Defaults to None.
        current_round (Optional[int], optional): Which self-cal imaging round. If None 'noselfcal'. Defaults to None.
        additional_linmos_suffix_str (Optional[str], optional): An additional string added to the end of the auto-generated linmos base name. Defaults to None.

    Returns:
        List[LinmosCommand]: The collection of linmos commands executed.
    """
    parsets: List[LinmosCommand] = []

    # Come up with the linmos suffix to add to output file
    suffixes = [f"round{current_round}" if current_round else "noselfcal"]
    if additional_linmos_suffix_str:
        suffixes.insert(0, additional_linmos_suffix_str)

    main_linmos_suffix_str = ".".join(suffixes)

    todo: List[Tuple[Any, str]] = [(None, get_beam_resolution_str(mode="optimal"))]
    if field_options.fixed_beam_shape:
        logger.info(
            f"Creating second round of linmos images with {field_options.fixed_beam_shape}"
        )
        todo.append(
            (field_options.fixed_beam_shape, get_beam_resolution_str(mode="fixed"))
        )

    for round_beam_shape, beam_str in todo:
        linmos_suffix_str = f"{beam_str}.{main_linmos_suffix_str}"
        convol_suffix_str = f"{beam_str}.conv"

        beam_shape = task_get_common_beam.submit(
            wsclean_cmds=wsclean_cmds,
            cutoff=field_options.beam_cutoff,
            filter=".MFS.",
            fixed_beam_shape=round_beam_shape,
        )
        # NOTE: The order matters here. The last linmos file is used
        # when running the source finding. Putting this order around means
        # we would source find on the residual image
        if field_options.linmos_residuals:
            parsets.append(
                _convolve_linmos(
                    wsclean_cmds=wsclean_cmds,
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
            _convolve_linmos(
                wsclean_cmds=wsclean_cmds,
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


def _create_convolve_linmos_cubes(
    wsclean_cmds: Collection[WSCleanCommand],
    field_options: FieldOptions,
    current_round: Optional[int] = None,
    additional_linmos_suffix_str: Optional[str] = "cube",
):
    suffixes = [f"round{current_round}" if current_round else "noselfcal"]
    if additional_linmos_suffix_str:
        suffixes.insert(0, additional_linmos_suffix_str)
    linmos_suffix_str = ".".join(suffixes)

    beam_shapes = task_get_cube_common_beam.submit(
        wsclean_cmds=wsclean_cmds, cutoff=field_options.beam_cutoff
    )
    convolved_cubes = task_convolve_cube.map(
        wsclean_cmd=wsclean_cmds,  # type: ignore
        cutoff=field_options.beam_cutoff,
        mode=unmapped("image"),  # type: ignore
        beam_shapes=unmapped(beam_shapes),  # type: ignore
    )

    assert field_options.yandasoft_container is not None
    parset = task_linmos_images.submit(
        images=convolved_cubes,  # type: ignore
        container=field_options.yandasoft_container,
        suffix_str=linmos_suffix_str,
        holofile=field_options.holofile,
        cutoff=field_options.pb_cutoff,
        filter=None,
    )
    return parset


@task
@wrapper_options_from_strategy(update_options_keyword="update_masking_options")
def task_create_image_mask_model(
    image: Union[LinmosCommand, ImageSet, WSCleanCommand],
    image_products: AegeanOutputs,
    update_masking_options: Optional[Dict[str, Any]] = None,
) -> FITSMaskNames:
    """Create a mask from a linmos image, with the intention of providing it as a clean mask
    to an appropriate imager. This is derived using a simple signal to noise cut.

    Args:
        linmos_parset (LinmosCommand): Linmos command and associated meta-data
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
    if isinstance(image, LinmosCommand):
        source_image = image.image_fits
    elif isinstance(image, ImageSet) and image.image is not None:
        source_image = list(image.image)[-1]
    elif isinstance(image, WSCleanCommand) and image.imageset is not None:
        source_image = list(image.imageset.image)[-1]
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
    linmos_mask_names: FITSMaskNames, wsclean_cmd: WSCleanCommand
) -> FITSMaskNames:
    """Extract a clean mask for a beam from a larger clean mask (e.g. derived from a field)

    Args:
        linmos_mask_names (FITSMaskNames): Mask that will be drawn from to form a smaller clean mask (e.g. for a beam)
        wsclean_cmd (WSCleanCommand): Wsclean command and meta-data. This is used to draw from the WCS to create an appropriate pixel-to-pixel mask

    Returns:
        FITSMaskNames: Clean mask for a image
    """
    # All images made by wsclean will have the same WCS
    assert (
        wsclean_cmd.imageset is not None
    ), f"{wsclean_cmd.imageset=}, which should not happen"
    beam_image = list(wsclean_cmd.imageset.image)[0]
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
            image_path=plot_path, description=f"Validation plot {str(plot_path)}"
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
                df = pd.read_csv(table)
                df_dict = df.to_dict("records")
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
                    df = pd.read_csv(subtable)
                    df_dict = df.to_dict("records")
                    create_table_artifact(
                        table=df_dict,
                        description=f"{subtable.stem}",
                    )  # type: ignore

    return validation_tables


def _validation_items(
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
