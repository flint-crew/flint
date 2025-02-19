"""Basic utilities to load operational parameters from a yaml-based
configuration file. The idea being that a configuration file would
be used to specify the options for imaging and self-calibration
throughout the pipeline.
"""

from __future__ import annotations

import shutil
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

import yaml

from flint.imager.wsclean import WSCleanOptions
from flint.logging import logger
from flint.masking import MaskingOptions
from flint.naming import add_timestamp_to_path
from flint.options import ArchiveOptions
from flint.selfcal.casa import GainCalOptions
from flint.source_finding.aegean import AegeanOptions, BANEOptions

# TODO: It feels like that the standard of this strategy file should
# be updated. In its current form the "initial" section is required, with
# with the subsequent selfcal section being used for continuum imaging.
# Should this be intended to be used for other pipelines it would be
# better to have an "imager" operation mode, and put these two things
# into there

# Known headers must **always** be present in the strategy file
KNOWN_HEADERS = ("defaults", "version")
# Known options are optional, but if present must be in the correct format
KNOWN_OPERATIONS = ("selfcal", "stokesv", "subtractcube", "polarisation")
FORMAT_VERSION = 0.2
MODE_OPTIONS_MAPPING = {
    "wsclean": WSCleanOptions,
    "gaincal": GainCalOptions,
    "masking": MaskingOptions,
    "archive": ArchiveOptions,
    "bane": BANEOptions,
    "aegean": AegeanOptions,
}
POLARISATION_MAPPING = {
    "total": "i",
    "linear": "qu",
    "circular": "v",
}


def _create_mode_mapping_defaults() -> dict[str, Any]:
    """Create the default key-values for each of the registered Option classes

    Returns:
        Dict[str, Any]: Name of mode and the supported keys and default values for each
    """
    return {k: i()._asdict() for k, i in MODE_OPTIONS_MAPPING.items()}


# A simple representation to pass around. Will help the type
# analysis and future pirates be clear with their mutinous
# intentions
class Strategy(dict):
    """Base representation for handling a loaded flint
    strategy"""

    pass


def copy_and_timestamp_strategy_file(output_dir: Path, input_yaml: Path) -> Path:
    """Timestamp and copy the input strategy file to an
    output directory

    Args:
        output_dir (Path): Output directory the file will be copied to
        input_yaml (Path): The file to copy

    Returns:
        Path: Copied and timestamped file path
    """
    stamped_imaging_strategy = (
        output_dir / add_timestamp_to_path(input_path=input_yaml).name
    )
    logger.info(f"Copying {input_yaml.absolute()} to {stamped_imaging_strategy}")
    shutil.copyfile(input_yaml.absolute(), stamped_imaging_strategy)

    return Path(stamped_imaging_strategy)


def load_and_copy_strategy(
    output_split_science_path: Path, imaging_strategy: Path | None = None
) -> Strategy | None:
    """Load a strategy file and copy a timestamped version into the output directory
    that would contain the science processing.

    Args:
        output_split_science_path (Path): Where the strategy file should be copied to (where the data would be processed)
        imaging_strategy (Optional[Path], optional): Location of the strategy file. Defaults to None.

    Returns:
        Union[Strategy, None]: The loadded strategy file if provided, `None` otherwise
    """
    return (
        load_strategy_yaml(
            input_yaml=copy_and_timestamp_strategy_file(
                output_dir=output_split_science_path,
                input_yaml=imaging_strategy,
            ),
            verify=True,
        )
        if imaging_strategy
        else None
    )


def get_selfcal_options_from_yaml(input_yaml: Path | None = None) -> dict:
    """Stub to represent interaction with a configurationf ile

    If a path is supplied, an error is raised.

    Args:
        input_yaml (Optional[Path], optional): Path to the configuration file. . Defaults to Optional[Path]=None.

    Returns:
        Dict: Mapping where the key is the self-calibration round, and values are key-value of updated gaincal options
    """

    assert input_yaml is None, (
        "Configuring via a yaml configuration file is not yet support. "
    )

    return {
        1: {"solint": "60s", "uvrange": ">235m", "nspw": 1},
        2: {"solint": "30s", "calmode": "p", "uvrange": ">235m", "nspw": 4},
        3: {"solint": "60s", "calmode": "ap", "uvrange": ">235m", "nspw": 4},
        4: {"solint": "30s", "calmode": "ap", "uvrange": ">235m", "nspw": 4},
    }


def get_image_options_from_yaml(
    input_yaml: Path | None = None, self_cal_rounds: bool = False
) -> dict:
    """Stub to interact with configuration file.

    If a `input_yaml` file is provided an error is raised

    Args:
        input_yaml (Optional[Path], optional): Should be None. Defaults to None.
        self_cal_rounds (bool, optional): Whether options for first imaging is being provided, or options to supply for each self-cal round. Defaults to False.

    Returns:
        Dict: _description_
    """

    assert input_yaml is None, (
        "Configuring via a yaml configuration file is not yet supported. "
    )

    MULTISCALE_SCALES = (0, 15, 30, 40, 50, 60, 70, 120)
    IMAGE_SIZE = 7144

    if not self_cal_rounds:
        return {
            "size": IMAGE_SIZE,
            "minuvw_m": 235,
            "weight": "briggs -1.5",
            "scale": "2.5arcsec",
            "nmiter": 10,
            "deconvolution_channels": 4,
            "fit_spectral_pol": 3,
            "auto_mask": 10,
            "multiscale": True,
            "local_rms_window": 55,
            "multiscale_scales": MULTISCALE_SCALES,
        }
    else:
        return {
            1: {
                "size": IMAGE_SIZE,
                "weight": "briggs -1.5",
                "scale": "2.5arcsec",
                "nmiter": 20,
                "minuvw_m": 235,
                "channels_out": 18,
                "deconvolution_channels": 6,
                "fit_spectral_pol": 3,
                "auto_mask": 8.0,
                "local_rms_window": 55,
                "multiscale_scales": MULTISCALE_SCALES,
            },
            2: {
                "size": IMAGE_SIZE,
                "weight": "briggs -1.5",
                "scale": "2.5arcsec",
                "multiscale": True,
                "minuvw_m": 235,
                "nmiter": 20,
                "channels_out": 18,
                "deconvolution_channels": 6,
                "fit_spectral_pol": 3,
                "auto_mask": 7.0,
                "local_rms_window": 55,
                "multiscale_scales": MULTISCALE_SCALES,
            },
            3: {
                "size": IMAGE_SIZE,
                "weight": "briggs -1.5",
                "scale": "2.5arcsec",
                "multiscale": True,
                "minuvw_m": 235,
                "nmiter": 20,
                "channels_out": 18,
                "deconvolution_channels": 6,
                "fit_spectral_pol": 3,
                "auto_mask": 6.0,
                "local_rms_window": 55,
                "multiscale_scales": MULTISCALE_SCALES,
            },
            4: {
                "size": IMAGE_SIZE,
                "weight": "briggs -1.5",
                "scale": "2.5arcsec",
                "multiscale": True,
                "minuvw_m": 235,
                "nmiter": 20,
                "channels_out": 16,
                "deconvolution_channels": 4,
                "fit_spectral_pol": 3,
                "auto_mask": 8,
                "local_rms_window": 55,
                "multiscale_scales": MULTISCALE_SCALES,
            },
            5: {
                "size": IMAGE_SIZE,
                "weight": "briggs -1.5",
                "scale": "2.5arcsec",
                "multiscale": True,
                "minuvw_m": 235,
                "nmiter": 20,
                "channels_out": 4,
                "fit_spectral_pol": 3,
                "auto_mask": 7.0,
                "local_rms_window": 55,
                "multiscale_scales": MULTISCALE_SCALES,
            },
        }


def get_options_from_strategy(
    strategy: Strategy | None | Path,
    operation: str,
    mode: str = "wsclean",
    round_info: int | None = None,
    max_round_override: bool = True,
    polarisation: str | None = None,
) -> dict[Any, Any]:
    f"""Extract a set of options from a strategy file to use in a pipeline
    run. If the mode exists in the default section, these are used as a base.

    If the mode exists and a round is specified, the options listed in the
    round are used to update the defaults.

    The default `operation` of `None` implies options related to imaging are
    requested. if a `operation` is provided then options for that mode are
    retrieved. These are ones that do not vary as across rounds of self-calibration.
    An `operation` can have a `mode`, such as `stokesv` requiring a `wsclean` mode. Acceptable
    `operation` values are {KNOWN_OPERATIONS}

    Args:
        strategy (Union[Strategy,None,Path]): A loaded instance of a strategy file. If `None` is provided then an empty dictionary is returned. If `Path` attempt to load the strategy file.
        mode (str, optional): Which set of options to load. Typical values are `wsclean`, `gaincal` and `masking`. Defaults to "wsclean".
        round_info (Union[str, int], optional): Which round to load options for. May be `initial` or an `int` (which indicated a self-calibration round). Defaults to "initial".
        max_round_override (bool, optional): Check whether an integer round number is recorded. If it is higher than the largest self-cal round specified, set it to the last self-cal round. If False this is not performed. Defaults to True.
        operation (Optional[str], optional): Get options related to a specific operation. Defaults to None.

    Raises:
        ValueError: An unrecongised value for `round`.
        AssertError: An unrecongised value for `round`.

    Returns:
        Dict[Any, Any]: Options specific to the requested set
    """

    if strategy is None:
        return {}
    elif isinstance(strategy, Path):
        strategy = load_strategy_yaml(input_yaml=strategy)

    # Some sanity checks
    assert isinstance(strategy, (Strategy, dict)), (
        f"Unknown input strategy type {type(strategy)}"
    )
    assert round_info is None or isinstance(round_info, int), (
        f"{round_info=} not a known value or type. "
    )
    if operation not in KNOWN_OPERATIONS:
        raise ValueError(
            f"{operation=} is not recognised. Known operations are {KNOWN_OPERATIONS}"
        )

    # Override the round if requested
    if (
        isinstance(round_info, int)
        and max_round_override
        and "selfcal" in strategy.keys()
    ):
        round_info = min(round_info, max(strategy["selfcal"].keys()))

    # step one, get the defaults
    options = dict(**strategy["defaults"][mode]) if mode in strategy["defaults"] else {}
    logger.debug(f"Defaults for {mode=}, {options=}")

    # A default empty dict
    update_options = {}

    assert operation in strategy, f"{operation=} not in {strategy.keys()}"

    operation_scope = strategy.get(operation)
    if round_info is not None:
        if not isinstance(operation_scope, dict):
            raise ValueError(
                f"{operation_scope=} is not a dictionary. Cannot extract {round_info=}"
            )
        operation_scope = operation_scope[round_info]

    # Override the polarisation if requested
    if polarisation is not None:
        if not isinstance(operation_scope, dict):
            raise ValueError(
                f"{operation_scope=} is not a dictionary. Cannot extract {polarisation=}"
            )
        operation_scope = operation_scope[polarisation]
        # Update the wsclean options with the polarisation mapping
        if mode == "wsclean":
            operation_scope.setdefault(mode, {}).update(
                {"pol": POLARISATION_MAPPING[polarisation]}
            )

    if not isinstance(operation_scope, dict):
        raise ValueError(
            f"{operation_scope=} is not a dictionary. Cannot extract {round_info=}"
        )

    if mode in operation_scope:
        update_options = dict(**operation_scope[mode])

    if update_options:
        logger.debug(f"Updating options with {update_options=}")
        options.update(update_options)

    return options


def verify_configuration(input_strategy: Strategy, raise_on_error: bool = True) -> bool:
    """Perform basic checks on the configuration file

    Args:
        input_strategy (Strategy): The loaded configuration file structure
        raise_on_error (bool, optional): Whether to raise an error should an issue in thew config file be found. Defaults to True.

    Raises:
        ValueError: Whether structure is valid

    Returns:
        bool: Config file is not valid. Raised only if `raise_on_error` is `True`
    """

    errors: list[str] = []

    for known_header in KNOWN_HEADERS:
        if known_header not in input_strategy.keys():
            errors.append(
                f"Required section header {known_header} missing from input configuration."
            )

    if "version" in input_strategy.keys():
        if input_strategy["version"] != FORMAT_VERSION:
            errors.append(
                f"Version mismatch. Expected {FORMAT_VERSION}, got {input_strategy['version']}"
            )

    # make sure the main components of the file are there
    unknown_headers = [
        header
        for header in input_strategy.keys()
        if header not in KNOWN_HEADERS and header not in KNOWN_OPERATIONS
    ]

    if unknown_headers:
        errors.append(f"{unknown_headers=} found. Supported headers: {KNOWN_HEADERS}")

    if "selfcal" in input_strategy:
        round_keys = input_strategy["selfcal"].keys()

        if not all([isinstance(i, int) for i in round_keys]):
            errors.append("The keys into the self-calibration should be ints. ")

        for round_info in round_keys:
            for mode in input_strategy["selfcal"][round_info]:
                try:
                    options = get_options_from_strategy(
                        strategy=input_strategy,
                        operation="selfcal",
                        mode=mode,
                        round_info=round_info,
                    )
                    try:
                        _ = MODE_OPTIONS_MAPPING[mode](**options)
                    except TypeError as typeerror:
                        errors.append(
                            f"{mode=} mode in {round_info=} incorrectly formed. {typeerror} "
                        )
                except Exception as exception:
                    errors.append(f"{exception}")

    if "polarisation" in input_strategy:
        _supported_polarisations = tuple(POLARISATION_MAPPING.keys())
        polarisations = input_strategy["polarisation"].keys()
        for polarisation in polarisations:
            if polarisation not in _supported_polarisations:
                errors.append(f"{polarisation=} not in {_supported_polarisations}. ")
            for mode in input_strategy["polarisation"][polarisation]:
                try:
                    options = get_options_from_strategy(
                        strategy=input_strategy,
                        operation="polarisation",
                        mode=mode,
                    )
                    try:
                        _ = MODE_OPTIONS_MAPPING[mode](**options)
                    except TypeError as typeerror:
                        errors.append(
                            f"{mode=} mode in {round_info=} incorrectly formed. {typeerror} "
                        )
                except Exception as exception:
                    errors.append(f"{exception}")

    for operation in KNOWN_OPERATIONS:
        # Already checked above
        if operation == "selfcal" or operation == "polarisation":
            continue
        if operation in input_strategy.keys():
            for mode in input_strategy[operation]:
                try:
                    options = get_options_from_strategy(
                        strategy=input_strategy, mode=mode, operation=operation
                    )
                    try:
                        _ = MODE_OPTIONS_MAPPING[mode](**options)
                    except TypeError as typeerror:
                        errors.append(
                            f"{mode=} mode in {operation=} incorrectly formed. {typeerror} "
                        )
                except Exception as exception:
                    errors.append(f"{exception}")

    valid_config = len(errors) == 0
    if not valid_config:
        for error in errors:
            logger.warning(error)

        if raise_on_error:
            raise ValueError("Configuration file not valid. ")

    return valid_config


def load_strategy_yaml(input_yaml: Path, verify: bool = True) -> Strategy:
    """Load in a flint based configuration file, which
    will be used to form the strategy for imaging of
    a field.

    The format of the return is likely to change. This
    is not to be relied on for the moment, and should
    be considered a toy. There will be a mutiny.

    Args:
        input_yaml (Path): The imaging strategy to use
        verify (bool, optional): Apply some basic checks to ensure a correctly formed strategy. Defaults to True.

    Returns:
        Strategy: The parameters of the imaging and self-calibration to use.
    """

    logger.info(f"Loading {input_yaml} file. ")

    with open(input_yaml) as in_file:
        input_strategy = Strategy(yaml.load(in_file, Loader=yaml.Loader))

    if verify:
        verify_configuration(input_strategy=input_strategy)

    return input_strategy


def write_strategy_to_yaml(strategy: Strategy, output_path: Path) -> Path:
    """Write the contents of a current strategy to a yaml file

    Args:
        strategy (Strategy): The strategy to write out
        output_path (Path): Where to write the output YAML file to

    Returns:
        Path: The path the output YAML file was written to
    """

    logger.info(f"Writing strategy to {output_path=}")

    with open(output_path, "w") as out_file:
        yaml.dump(data=strategy, stream=out_file, sort_keys=False)

    return output_path


# TODO: Create the file only for a subset of known defaults
def create_default_yaml(output_yaml: Path, selfcal_rounds: int | None = None) -> Path:
    """Create an example strategy yaml file that outlines the options to use at varies stages
    of some assumed processing pipeline.

    This is is completely experimental, and expected fields might change.

    Args:
        output_yaml (Path): Location to write the yaml file to.
        selfcal_rounds (Optional[int], optional): Will specify the number of self-calibration loops to include the file. If None, there will be none written. Defaults to None.

    Returns:
        Path: Path to the written yaml output file.
    """
    logger.info("Generating a default strategy. ")
    strategy: dict[Any, Any] = {}

    strategy["version"] = FORMAT_VERSION

    strategy["defaults"] = _create_mode_mapping_defaults()

    if selfcal_rounds:
        logger.info(f"Creating {selfcal_rounds} self-calibration rounds. ")
        selfcal: dict[int, Any] = {}
        for selfcal_round in range(0, selfcal_rounds):
            selfcal[selfcal_round] = {
                "wsclean": {},
                "gaincal": {},
                "masking": {},
            }

        strategy["selfcal"] = selfcal

    with open(output_yaml, "w") as out_file:
        logger.info(f"Writing {output_yaml}.")
        yaml.dump(data=strategy, stream=out_file, sort_keys=False)

    return output_yaml


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="Tooling to interact with flint yaml configuration files. "
    )

    subparser = parser.add_subparsers(dest="mode")

    create_parser = subparser.add_parser(
        "create", help="Create an initial yaml file for editing. "
    )
    create_parser.add_argument(
        "output_yaml",
        type=Path,
        default="flint_strategy.yaml",
        help="The output YAML file to write with default options for various stages. ",
    )
    create_parser.add_argument(
        "--selfcal-rounds",
        type=int,
        default=None,
        help="Number of self-calibration rounds to use. ",
    )

    load_parser = subparser.add_parser("load")
    load_parser.add_argument(
        "input_yaml",
        type=Path,
        help="Path to a strategy yaml file to load and inspect. ",
    )
    verify_parser = subparser.add_parser(
        "verify", help="Verify a yaml file is correct, as far as we can tell.  "
    )
    verify_parser.add_argument("input_yaml", type=Path, help="Path to a strategy file")

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    if args.mode == "create":
        create_default_yaml(
            output_yaml=args.output_yaml, selfcal_rounds=args.selfcal_rounds
        )
    elif args.mode == "load":
        load_strategy_yaml(input_yaml=args.input_yaml)
    elif args.mode == "verify":
        input_strategy = load_strategy_yaml(input_yaml=args.input_yaml)
        try:
            if verify_configuration(input_strategy=input_strategy):
                logger.info(f"{args.input_yaml} appears valid")
        except ValueError:
            logger.info(f"{args.input_yaml} does not appear to be valid")
    else:
        logger.error(f"{args.mode=} is not set or not known. Check --help. ")


if __name__ == "__main__":
    cli()
