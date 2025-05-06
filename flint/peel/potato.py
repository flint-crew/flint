"""Utilities the connect to Stefan Duchesne potatopeel module, which is responsible for
peeling sources in racs. The repository is available at:

https://gitlab.com/Sunmish/potato/-/tree/main

Potato stands for "Peel Out That Annoying Terrible Object".

Although this is a python module, for the moment it is expected
to be in a singularity container. There are several reasons
for this, but the principal one is that the numba module used
by potatopeel may be difficult to get working correctly alongside
dask and flint. Keeping it simple at this point is the main aim.
There is also the problem of casatasks + python-casacore not
jiving in newer python versions.

"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Any, Collection, NamedTuple

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.table import Table
from casacore.tables import table

from flint.imager.wsclean import WSCleanOptions
from flint.logging import logger
from flint.ms import get_freqs_from_ms, get_phase_dir_from_ms
from flint.naming import get_potato_output_base_path
from flint.options import MS
from flint.sclient import run_singularity_command
from flint.utils import (
    create_directory,
    generate_strict_stub_wcs_header,
    get_packaged_resource_path,
)


class PotatoConfigOptions(NamedTuple):
    """Container class to hold options that go into the potatopy
    configuration creation software by Stefan Duchesne. See:

    https://gitlab.com/Sunmish/potato
    """

    image_size: int = 6148
    """Size of an in-field image"""
    image_scale: float = 0.0006944
    """The pixel scale of the in-field image in degrees"""
    image_briggs: float = -1.5
    """Briggs robust parameter for the in-field image"""
    image_channels: int = 4
    """Number of output channels for the in-field image"""
    image_minuvl: float = 700
    """"Minimum (u,v)- distance in wavelengths for data to be selected"""
    peel_size: int = 1000
    """Size of the peel image to make, in pixels"""
    peel_scale: float = 0.0006944
    """Pixel scale of the peel images in degrees"""
    peel_channels: int = 16
    """Number of output channels for the peel images"""
    peel_nmiter: int = 7
    """Number of major iterations allowed for the peel sources"""
    peel_minuvl: float = 700
    """"Minimum (u,v)- distance in wavelengths for data to be selected for the peel image"""
    peel_multiscale: bool = True
    """Whether multi-scale is to be used for the peel sources"""


class PotatoPeelArguments(NamedTuple):
    """The mandatory arguments for potato peel."""

    # These are kept separatte from the PotatoPeelOptions
    # class so the PotatoPeelOptions may be written to a template file
    ms: Path
    """The measurement set that will be examined for peeling"""
    ras: Collection[float]
    """The source RA in degrees to peel"""
    decs: Collection[float]
    """The source Dec in degrees to peel"""
    peel_fovs: Collection[float]
    """The field-of-views that should be created for the peel source in degrees"""
    image_fov: float
    """The field-of-view in degrees of the main in-field image. If a sources is within this radius it is not peeled (because it would be imaged)"""
    n: Collection[str]
    """Name of the source being peeled"""


class PotatoPeelOptions(NamedTuple):
    """Container class to hold options that go to the potato peel
    software by Stefan Duchesne. FLINT uses the `hot_potato` version.
    ee:

    https://gitlab.com/Sunmish/potato
    """

    c: Path | None = None
    """Path to the potatopeel configuration file"""
    solint: float = 30
    """Solution interval to use when applying gaincal"""
    calmode: str = "P"
    """Self-calibration mode to use (see casatasks gaincal)"""
    minpeelflux: float = 0.5
    """Minimum flux, in Jy, for the peeling procedure (image->selfcal->image)"""
    refant: int = 1
    """Reference antenna to use when solving for self-cal solutions"""
    direct_subtract: bool = True
    """Whether a direct model subtraction (without self-cal) should be used ift he source is faint"""
    intermediate_peels: bool = True
    """Creates an image after each calibration and subtraction loop to show iterative improvements of the subject peel source"""
    T: str | Path = "peel"
    """Where the temporary wsclean files will be written to"""
    minuvimage: float | None = None
    """The minimum uv distance in wavelengths to use for imaging"""
    minuvpeel: float | None = None
    """The minimum uv distance in wavelengths to use when attempting to self-calibrate"""

    def with_options(self, **kwargs) -> PotatoPeelOptions:
        items = self._asdict()
        items.update(**kwargs)
        return PotatoPeelOptions(**items)


def load_known_peel_sources() -> Table:
    """Locate and load the packaged set of known sources to peel. These sources
    are drawn from Duchesne et. al. (2023), Table 3.

    Returns:
        Table: The astropy Table of candidate sources to remove.
    """
    peel_srcs_csv = get_packaged_resource_path("flint", "data/peel/known_sources.csv")

    logger.info(f"Loading in {peel_srcs_csv=}")
    peel_tab = Table.read(peel_srcs_csv)

    logger.info(f"Loaded {len(peel_tab)} sources. ")

    return peel_tab


def source_within_image_fov(
    source_coord: SkyCoord,
    beam_coord: SkyCoord,
    image_size: int,
    pixel_scale: u.Quantity | str,
) -> bool:
    """Evaluate whether a source will be within the field of view
    of an image.

    Args:
        source_coord (SkyCoord): The source position to consider
        beam_coord (SkyCoord): The center beam position to consider
        image_size (int): The image size. This assumes a square image.
        pixel_scale (Union[u.Quantity, str]): The pixel size, assuming square pixels.

    Returns:
        bool: Whether the source is expected to be in the field of view
    """
    # TODO: Allow the wcs to be passed in

    wcs = generate_strict_stub_wcs_header(
        position_at_image_center=beam_coord,
        image_shape=(image_size, image_size),
        pixel_scale=pixel_scale,
    )

    x, y = wcs.world_to_pixel(source_coord)

    # Since the reference pixel is at the image center, then the valid domain
    # is between 0 and image size
    source_in_image = 0 <= x < image_size and 0 <= y < image_size

    return source_in_image


def find_sources_to_peel(
    ms: MS,
    image_options: WSCleanOptions,
    field_idx: int = 0,
    maximum_offset: float = 30,
    minimum_apparent_brightness: float = 0.5,
    override_beam_position_with: SkyCoord | None = None,
) -> Table | None:
    """Obtain a set of sources to peel from a reference candidate set. This will
    evaluate whether a source should be peels based on two criteria:

    - if it is below a nominal primary beam cut off level (10 percent, assuming a gaussian primary beam)
    - if the sources is within some separation of the imaging center

    Args:
        ms (MS): The measurement set that is being considered
        image_options (WSCleanOptions): The imaging parameters that will be used to compute a placeholder WCS
        field_idx (int, optional): Which field in the MS to draw the position from. Defaults to 0.
        maximum_offset (float, optional): The largest separation, in degrees, before a source is ignored. Defaults to 30.0.
        minimum_apparent_brightness (float, optional): The minimum apparent brightnessm, in Jy, a source should be before attempting to peel. Defaults to 0.5.
        override_beam_position_with (Optional[SkyCoord], optional): Ignore the beam position of the input MS, instead use this. Do not rely on this option as it may be taken away. Defaults to None.

    Returns:
        Union[Table,None]: Collection of sources to peel from the reference table. Column names are Name, RA, Dec, Aperture. This is the package table. If no sources need to be peeled None is returned.
    """
    image_size, pixel_scale = None, None

    if isinstance(image_options, WSCleanOptions):
        image_size = image_options.size
        pixel_scale = image_options.scale
        logger.info("Replace known wsclean units with astropy.unit.Quantity version")
        pixel_scale = pixel_scale.replace("asec", "arcsec")
        pixel_scale = pixel_scale.replace("amin", "arcmin")
    else:
        raise TypeError(f"{type(image_options)=} is not known. ")

    logger.debug(f"Extracting image direction for {field_idx=}")
    image_coord = (
        get_phase_dir_from_ms(ms=ms)
        if override_beam_position_with is None
        else override_beam_position_with
    )

    logger.info(
        f"Considering sources to peel around {image_coord=}, {type(image_coord)=}"
    )

    peel_srcs_tab = load_known_peel_sources()

    freqs = get_freqs_from_ms(ms=ms)
    nominal_freq = np.mean(freqs) * u.Hz  # type: ignore
    logger.info(f"The nominal frequency is {nominal_freq.to(u.MHz)}")

    peel_srcs = []

    # TODO: Make this a mapping function
    for src in peel_srcs_tab:
        src_coord = SkyCoord(src["RA"], src["Dec"], unit=(u.degree, u.degree))
        offset = image_coord.separation(src_coord)
        if source_within_image_fov(
            source_coord=src_coord,
            beam_coord=image_coord,
            image_size=image_size,
            pixel_scale=pixel_scale,
        ):
            logger.debug(
                f"Source {src['Name']} within image field of view of image with {image_size}x{image_size} and {pixel_scale=}"
            )
            continue

        if offset > maximum_offset * u.deg:  # type: ignore
            continue

        logger.info(
            f"Source {src['Name']} is {offset} away, below {maximum_offset=} cutoff "
        )

        # At the moment no brightness is in the known_sources.csv
        # taper = generate_pb(
        #     pb_type="airy", freqs=nominal_freq, aperture=12 * u.m, offset=offset
        # )
        # assert isinstance(taper.atten, float), "Expected a float"

        # if taper.atten[0] * tab[] > cutoff:
        #     logger.info(
        #         f"{src['Name']} attenuation {taper.atten} is above {cutoff=} (in field of view)"
        #     )
        #     continue

        peel_srcs.append(src)

    return Table(rows=peel_srcs, names=peel_srcs_tab.colnames) if peel_srcs else None


def prepare_ms_for_potato(ms: MS) -> MS:
    """The potatopeel software requires the data column being operated against to be
    called DATA. This is a requirement of CASA and its gaincal / applysolution task.

    If there is already a DATA column and this is not the nominated data column described
    by the MS then it is removed, and the nominated data column is renamed.

    If the DATA column already exists and it is the nominated column described by
    MS then this is returned. If there is a CORRECTED_DATA column in this situation
    it is removed.

    Args:
        ms (MS): The measurement set that will be edited

    Raises:
        ValueError: Raised when a column name is expected but can not be found.

    Returns:
        MS: measurement set with the data column moved into the correct location
    """
    data_column = ms.column

    logger.info(f"The nominated column is: {data_column=}")
    logger.warning(
        "Deleting and renaming columns so final column is DATA. "
        "PotatoPeel only operates on the DATA column. "
    )

    # If the data column already exists and is the nominated column, then we should
    # just return, ya scally-wag
    if data_column == "DATA":
        logger.info(f"{data_column=} is already DATA. No need to rename. ")

        with table(str(ms.path), readonly=False, ack=False) as tab:
            if "CORRECTED_DATA" in tab.colnames():
                logger.info(f"Removing CORRECTED_DATA column from {ms.path}")
                tab.removecols("CORRECTED_DATA")

        return ms

    with table(str(ms.path), ack=False, readonly=False) as tab:
        colnames = tab.colnames()
        if data_column not in colnames:
            raise ValueError(
                f"Column {data_column} not found in {ms.path!s}. Columns found: {colnames}"
            )

        # In order to rename the data_column to DATA, we need to make sure that
        # there is not an existing DATA column
        if "DATA" in colnames:
            logger.info("Removing the existing DATA column")
            tab.removecols("DATA")

        logger.info(f"Renaming {data_column} to DATA")
        tab.renamecol(data_column, "DATA")

        # Update column names after the delete and rename
        colnames = tab.colnames()

        # Remove any CORRECT_DATA column, should it exist, as
        # potatopeel will create it
        if "CORRECTED_DATA" in colnames:
            logger.info(f"Removing CORRECTED_DATA column from {ms.path}")
            tab.removecols("CORRECTED_DATA")

    return ms.with_options(column="DATA")


def _potato_options_to_command(
    potato_options: PotatoPeelArguments | PotatoConfigOptions | PotatoPeelOptions,
    skip_keys: Collection[str] | None = None,
    check_double_keys: bool = False,
) -> str:
    """Construct the CLI options that would be provided to
    a potato peel CLI program

    Args:
        potato_options (Union[PotatoPeelArguments,PotatoConfigOptions, PotatoPeelOptions]): An instance of one of the option classes to draw from
        skip_keys (Optional[Collection[str]], optional): A collections of keys to ignore when build the CLI. If None all keys in the provided options instance are used. Defaults to None.
        check_double_leys (bool, optional): Some long form names in `hot_potato` are single dash while others are double dash. This is not the case in the config creation tool. This will check to see if the double should be used. Defaults to False.

    Raises:
        TypeError: When an unrecognised data type is found in the provided options class

    Returns:
        str: A string of the CLI options and keys
    """
    skip_keys = tuple(skip_keys) if skip_keys else tuple()

    DOUBLE = ("ras", "decs", "peel_fovs", "intermediate_peels", "direct_subtract")

    sub_options = ""
    for key, value in potato_options._asdict().items():
        flag = "--"
        if check_double_keys:
            flag = "--" if key in DOUBLE else "-"
        logger.debug(f"{key=} {value=} {type(value)=}")
        if key in skip_keys:
            logger.debug(f"{key=} in {skip_keys=}, skipping")
            continue
        if isinstance(value, bool):
            logger.debug("bool")
            if value:
                sub_options += f"{flag}{key} "
        elif isinstance(value, (tuple, list)):
            logger.debug("tuple or list")
            out_value = " ".join([f"{v}" for v in value])
            sub_options += f"{flag}{key} {out_value} "
        elif isinstance(value, (int, float, str)):
            logger.debug("int flot str")
            sub_options += f"{flag}{key} {value} "
        elif isinstance(value, Path):
            logger.debug("Path")
            sub_options += f"{flag}{key} {value!s} "
        elif value is None:
            continue
        else:
            raise TypeError(f"Unrecognised {key=} {value=} type")

    return sub_options


class PotatoConfigCommand(NamedTuple):
    """Container for potato configuration command results"""

    config_path: Path
    """Path to the configuration file generated"""
    command: str
    """The command string that should be executed"""


def _potato_config_command(
    config_path: Path, potato_config_options: PotatoConfigOptions
) -> PotatoConfigCommand:
    """Create the peel_configuration.py command that will be called
    in the potato singularity image. This is the CLI version of the
    code (not calling the python function).

    Args:
        config_path (Path): Output location of the configuration file
        potato_config_options (PotatoConfigOptions): Instance of all the options to use

    Returns:
        PotatoconfigCommand: The CLI command that will be executed to create a potato configuration file
    """

    command = f"peel_configuration.py {config_path!s} "

    sub_options = _potato_options_to_command(potato_options=potato_config_options)
    command = command + sub_options

    logger.debug(f"Constructed command {command}")
    return PotatoConfigCommand(config_path=config_path, command=command)


def create_run_potato_config(
    potato_container: Path,
    ms_path: Path | MS,
    potato_config_options: PotatoConfigOptions,
) -> PotatoConfigCommand:
    """Construct and run a CLI command into the `peel_configuration.py`
    script of the `potatopeel` package.

    Args:
        potato_container (Path): Container with the `potatopeel` package installed
        ms_path (Union[Path, MS]): Path to the measurement set that will be peeled
        potato_config_options (PotatoConfigOptions): Options to tweak the values in the peel configuration

    Returns:
        PotatoConfigCommand: Container of the path to the peel configuration file and the corresponding command that generated it
    """

    ms = MS.cast(ms=ms_path)
    base_potato_path = get_potato_output_base_path(ms_path=ms.path)
    config_path = base_potato_path.parent / (base_potato_path.name + ".config")
    potato_config_command = _potato_config_command(
        config_path=config_path, potato_config_options=potato_config_options
    )

    run_singularity_command(
        image=potato_container,
        command=potato_config_command.command,
        bind_dirs=potato_config_command.config_path.parent,
    )

    return potato_config_command


class PotatoPeelCommand(NamedTuple):
    """Container to hold the items of the hot potato command"""

    ms: MS
    """The measurement set that potato has operated against"""
    command: str
    """The hot potato command that will be executed"""


def _potato_peel_command(
    ms: MS,
    potato_peel_arguments: PotatoPeelArguments,
    potato_peel_options: PotatoPeelOptions,
) -> PotatoPeelCommand:
    """Construct the CLI command for `hot_potato`, and appropriately
    handle the mandatory and optional arguments.

    Args:
        ms (MS): The measurement set that will be peeled
        potato_peel_arguments (PotatoPeelArguments): The mandatory arguments for `hot_potato`
        potato_peel_options (PotatoPeelOptions): The `hot_potato` options to supply

    Returns:
        PotatoPeelCommand: The `hot_potato` command that was constructed from the input `PotatoPeelOptions`
    """

    command = (
        f"hot_potato {ms.path.absolute()!s} {potato_peel_arguments.image_fov:.4f} "
    )

    for item in [potato_peel_arguments, potato_peel_options]:
        # The skip keys handle the mandatory arguments that are specified above
        sub_options = _potato_options_to_command(
            potato_options=item,  # type: ignore
            skip_keys=("image_fov", "ms"),
            check_double_keys=True,
        )
        command += sub_options

    return PotatoPeelCommand(ms=ms, command=command)


def create_run_potato_peel(
    potato_container: Path,
    ms: MS,
    potato_peel_arguments: PotatoPeelArguments,
    potato_peel_options: PotatoPeelOptions,
) -> PotatoPeelCommand:
    """Construct and run a `hot_potato` command to peel out sources from
    a measurement set.

    Args:
        potato_container (Path): Container with the potato peel software and appropriate tools (including wsclean)
        ms (MS): The measurement set that contains sources to peel
        potato_peel_arguments (PotatoPeelArguments): The mandatory arguments that go into `hot_potato`.
        potato_peel_options (PotatoPeelOptions): Options that are supplied to `hot_potato`.

    Returns:
        PotatoPeelCommand: The executed `hot_potato` command
    """

    # Construct the command
    potato_peel_command = _potato_peel_command(
        ms=ms,
        potato_peel_arguments=potato_peel_arguments,
        potato_peel_options=potato_peel_options,
    )

    # make sure the container can bind to all necessary directories. This
    # includes the potential directory used by wsclean to temporarily store
    # files
    # and the ms.path.parent directory which hosts the potato peel config file
    bind_dirs = [ms.path, ms.path.parent]
    if potato_peel_options.T is not None:
        if not Path(potato_peel_options.T).exists():
            create_directory(directory=Path(potato_peel_options.T))
        bind_dirs.append(Path(potato_peel_options.T))

    # Now run the command and hope for the best you silly pirate
    run_singularity_command(
        image=potato_container, command=potato_peel_command.command, bind_dirs=bind_dirs
    )

    return potato_peel_command


class NormalisedSources(NamedTuple):
    """Temporary container to hold the normalised source properties that
    would be provided to potato
    """

    source_ras: tuple[float]
    """The RAs in degrees"""
    source_decs: tuple[float]
    """The Decs in degrees"""
    source_fovs: tuple[float]
    """The size of each source to image in degrees"""
    source_names: tuple[str]
    """The name of each source"""


def get_source_props_from_table(table: Table) -> NormalisedSources:
    """Given the astropy table of known sources to peel, normalise their
    inputs for the potato peel CLI

    Args:
        table (Table): Table of sources to peel

    Returns:
        NormalisedSources: Collection of normalised sources properties that will be provided to `hot_potato`
    """

    sources_sky = SkyCoord(table["RA"], table["Dec"], unit=(u.deg, u.deg))

    source_ras = [source_sky.ra.deg for source_sky in sources_sky]  # type: ignore
    source_decs = [source_sky.dec.deg for source_sky in sources_sky]  # type: ignore
    source_apertures = (size for size in (table["Aperture"] * u.arcmin).to(u.deg).value)  # type: ignore
    source_names = [i.replace(" ", "_") for i in table["Name"].value]  # type: ignore

    return NormalisedSources(
        source_ras=tuple(source_ras),
        source_decs=tuple(source_decs),
        source_fovs=tuple(source_apertures),
        source_names=tuple(source_names),
    )


def _print_ms_colnames(ms: MS) -> MS:
    """A dummy function to print colnames in a MS table"""
    ms = MS.cast(ms=ms)

    with table(str(ms.path)) as tab:
        colnames = tab.colnames()

    logger.debug(f"The MS column names are: {colnames=}")

    return ms


def potato_peel(
    ms: MS,
    potato_container: Path,
    update_potato_config_options: dict[str, Any] | None = None,
    update_potato_peel_options: dict[str, Any] | None = None,
    image_options: WSCleanOptions | None = None,
) -> MS:
    """Peel out sources from a measurement set using PotatoPeel. Candidate sources
    from a known list of sources (see Table 3 or RACS-Mid paper) are considered.

    Args:
        ms (MS): The measurement set to peel out known sources
        potato_container (Path): Location of container with potatopeel software installed
        update_potato_config_options (Optional[Dict[str, Any]], optional): A dictionary with values to use to update the default options within the `PotatoConfigOptions`. If None use the defaults. Defaults to None.
        update_potato_peel_options (Optional[Dict[str, Any]], optional): A dictionary with values to use to update the default options within the `PotatoPeelOptions`. If None use the defaults. Defaults to None.
        image_options (Optional[WSCleanOptions], optional): Any imaging options that should be used to determine if sources require peeling (e.g. image size, pixel size)

    Returns:
        MS: Updated measurement set
    """
    potato_container = potato_container.absolute()

    logger.info(f"Will attempt to peel the {ms=}")
    logger.info(f"Using the potato peel container {potato_container}")

    if image_options is None:
        logger.info("No supplied image options, using default WSCleanOptions()")
        image_options = WSCleanOptions()

    logger.info("Colnames before potato")
    _print_ms_colnames(ms=ms)

    peel_tab = find_sources_to_peel(
        ms=ms, image_options=image_options, maximum_offset=6
    )

    if peel_tab is None or len(peel_tab) == 0:
        logger.info("No sources to peel. ")
        return ms

    logger.info(f"Will be peeling {len(peel_tab)} objects: {peel_tab['Name']}")

    update_potato_config_options = (
        update_potato_config_options if update_potato_config_options else {}
    )
    potato_config_options = PotatoConfigOptions(**update_potato_config_options)
    potato_config_command = create_run_potato_config(
        potato_container=potato_container,
        ms_path=ms,
        potato_config_options=potato_config_options,
    )

    ms = prepare_ms_for_potato(ms=ms)

    normalised_source_props = get_source_props_from_table(table=peel_tab)
    potato_peel_arguments = PotatoPeelArguments(
        ms=ms.path,
        ras=normalised_source_props.source_ras,
        decs=normalised_source_props.source_decs,
        peel_fovs=normalised_source_props.source_fovs,
        image_fov=0.01,
        n=normalised_source_props.source_names,
    )

    potato_peel_options = PotatoPeelOptions(
        c=potato_config_command.config_path,
    )
    if update_potato_peel_options:
        potato_peel_options = potato_peel_options.with_options(
            **update_potato_peel_options
        )

    create_run_potato_peel(
        potato_container=potato_container,
        ms=ms,
        potato_peel_arguments=potato_peel_arguments,
        potato_peel_options=potato_peel_options,
    )

    logger.info("Column names after potato")
    _print_ms_colnames(ms=ms)

    return ms.with_options(column="DATA")


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="A simple interface into the potatopeel software"
    )

    subparser = parser.add_subparsers(dest="mode")

    check_parser = subparser.add_parser(
        "check", help="Check whether there are sources to peel from a measurement set"
    )

    check_parser.add_argument(
        "ms", type=Path, help="Path to the measurement set that will be considered"
    )
    check_parser.add_argument(
        "--image-size",
        type=int,
        default=8000,
        help="The number of pixels that make up a square image. Used to determine if a source is within FoV",
    )
    check_parser.add_argument(
        "--pixel-scale",
        type=str,
        default="2.5arcsec",
        help="The size of a pixel in an astropy-understood unit. Used to assess whether a source is within the image FoV. ",
    )

    list_parser = subparser.add_parser(  # noqa
        "list", help="List the known candidate sources available for peeling. "
    )

    peel_parser = subparser.add_parser(
        "peel", help="Attempt to peel sources using potatopeel"
    )
    peel_parser.add_argument(
        "ms", type=Path, help="Path the to the measurement set with a source to peel"
    )

    peel_parser.add_argument(
        "--potato-container",
        type=Path,
        default=Path("./potato.sif"),
        help="Path to the singularity container that represented potatopeel",
    )
    peel_parser.add_argument(
        "--data-column",
        type=str,
        default="DATA",
        help="The column name that contains data with a source that needs to be peeled",
    )
    peel_parser.add_argument(
        "--image-size",
        type=int,
        default=8000,
        help="The number of pixels that make up a square image. Used to determine if a source is within FoV",
    )
    peel_parser.add_argument(
        "--pixel-scale",
        type=str,
        default="2.5arcsec",
        help="The size of a pixel in an astropy-understood unit. Used to assess whether a source is within the image FoV. ",
    )

    return parser


def cli():
    parser = get_parser()

    args = parser.parse_args()

    if args.mode == "list":
        tab = load_known_peel_sources()
        logger.info("Known candidate sources to peel")
        logger.info(tab)

    elif args.mode == "check":
        ms = MS(path=args.ms)
        image_options = WSCleanOptions(size=args.image_size, scale=args.pixel_scale)

        tab = find_sources_to_peel(ms=ms, image_options=image_options)
        logger.info("Sources to peel")
        logger.info(tab)

    elif args.mode == "peel":
        ms = MS(path=args.ms, column=args.data_column)
        image_options = WSCleanOptions(size=args.image_size, scale=args.pixel_scale)

        potato_peel(
            ms=ms, potato_container=args.potato_container, image_options=image_options
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    cli()
