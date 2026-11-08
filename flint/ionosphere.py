"""Correct ionospheric Faraday rotation with spinifex.

Spinifex models the ionosphere from IONEX maps that must be downloaded. Worker
nodes are often offline, so the maps are fetched beforehand with
``flint_ionosphere download`` and only read from disk when correcting.
"""

from __future__ import annotations

from argparse import ArgumentParser
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import astropy.units as u
from astropy.utils import iers
from capn_crunch import add_options_to_parser, create_options_from_parser

from flint.dp3 import DP3ApplyCalOptions, dp3_applycal
from flint.logging import logger
from flint.ms import HistoryEntry, add_history_entry
from flint.options import MS, BaseOptions

if TYPE_CHECKING:
    from spinifex.ionospheric.tec_data import IonexOptions


class IonosphereOptions(BaseOptions):
    """Options for the spinifex ionospheric Faraday rotation correction"""

    ionex_directory: Path
    """Directory holding the IONEX maps, downloaded beforehand with ``flint_ionosphere download``"""
    server: Literal["chapman", "cddis", "igsiono"] = "chapman"
    """Server the IONEX maps come from"""
    prefix: str = "uqr"
    """Analysis centre of the IONEX maps"""
    solution: Literal["final", "rapid"] = "final"
    """IONEX solution type"""
    timestep: float | None = None
    """Seconds between rotation measure evaluations. None evaluates every integration"""
    output_column: str = "IONO_CORRECTED_DATA"
    """Column the corrected visibilities are written to"""


def get_ionex_options(options: IonosphereOptions, offline: bool) -> IonexOptions:
    """The spinifex IONEX options, which when offline can only read maps already on disk."""
    from spinifex.ionospheric.tec_data import IonexOptions

    # spinifex skips any map already in the output directory, and copies a
    # file:// URL rather than downloading it. A missing map then fails at once.
    url_stem = f"file://{options.ionex_directory.absolute()}" if offline else None
    return IonexOptions(
        server=options.server,
        prefix=options.prefix,
        solution=options.solution,
        output_directory=options.ionex_directory,
        url_stem=url_stem,
    )


def download_ionex(mss: Iterable[MS | Path], options: IonosphereOptions) -> list[Path]:
    """Download the IONEX maps the measurement sets need. Run where there is internet.

    Args:
        mss (Iterable[MS | Path]): The measurement sets to be corrected
        options (IonosphereOptions): Where to put the maps, and which to get

    Returns:
        list[Path]: The IONEX maps on disk
    """
    from spinifex.ionospheric.ionex_download import _download_ionex
    from spinifex.vis_tools.ms_tools import get_metadata_from_ms

    options.ionex_directory.mkdir(parents=True, exist_ok=True)
    ionex_options = get_ionex_options(options, offline=False)

    ionex_paths: set[Path] = set()
    for ms in mss:
        times = get_metadata_from_ms(MS.cast(ms).path).times
        # spinifex also reads the next day's map to smooth over midnight
        for day_times in (times, times + 1 * u.day):
            ionex_paths.update(_download_ionex(times=day_times, options=ionex_options))

    logger.info(f"IONEX maps in {options.ionex_directory}: {sorted(ionex_paths)}")
    return sorted(ionex_paths)


@contextmanager
def offline_astropy() -> Iterator[None]:
    """Keep astropy from downloading Earth orientation tables, using its bundled copy."""
    with (
        iers.conf.set_temp("auto_download", False),
        iers.conf.set_temp("iers_degraded_accuracy", "warn"),
    ):
        yield


def get_rm_h5parm(ms: MS | Path, options: IonosphereOptions) -> Path:
    """Write the ionospheric rotation measures of a measurement set to an h5parm.

    Only reads IONEX maps already in ``options.ionex_directory``.

    Args:
        ms (MS | Path): The measurement set
        options (IonosphereOptions): Where the IONEX maps are, and which to use

    Raises:
        FileNotFoundError: Raised if a needed IONEX map has not been downloaded

    Returns:
        Path: The h5parm, holding a ``rotationmeasure000`` soltab
    """
    from spinifex.h5parm_tools import write_rm_to_h5parm
    from spinifex.vis_tools.ms_tools import get_metadata_from_ms, get_rm_from_ms

    ms = MS.cast(ms)
    h5parm = ms.path.with_suffix(".spinifex.h5")
    # spinifex adds a new solset to an existing h5parm rather than replacing it
    h5parm.unlink(missing_ok=True)

    try:
        with offline_astropy():
            rms = get_rm_from_ms(
                ms_path=ms.path,
                timestep=options.timestep * u.s if options.timestep else None,
                use_stations="all",
                iono_model_name="ionex",
                **dict(get_ionex_options(options, offline=True)),
            )
    except FileNotFoundError as error:
        msg = (
            f"IONEX maps for {ms.path} are missing from {options.ionex_directory}. "
            f"Fetch them on a node with internet: "
            f"flint_ionosphere download {options.ionex_directory} {ms.path}"
        )
        raise FileNotFoundError(msg) from error

    write_rm_to_h5parm(
        rms,
        str(h5parm),
        source_dir=get_metadata_from_ms(ms.path).source,
    )
    return h5parm


def correct_ionosphere(ms: MS | Path, options: IonosphereOptions) -> MS:
    """Remove ionospheric Faraday rotation, writing to ``options.output_column``.

    Always reads the uncorrected column the measurement set tracks, so running
    this again redoes the correction rather than applying it twice.

    Args:
        ms (MS | Path): The measurement set, tracking its uncorrected column
        options (IonosphereOptions): Options for the correction

    Raises:
        ValueError: Raised if the tracked column is the output column

    Returns:
        MS: The measurement set tracking the corrected column
    """
    ms = MS.cast(ms)
    if ms.column is None:
        ms = ms.with_options(column="DATA")
    if ms.column == options.output_column:
        raise ValueError(
            f"{ms.path} already tracks {options.output_column}. "
            "Correcting it again would rotate the data twice."
        )

    logger.info(f"Correcting ionospheric Faraday rotation of {ms.path}")
    h5parm = get_rm_h5parm(ms, options)
    corrected_ms = dp3_applycal(
        ms,
        h5parm=h5parm,
        options=DP3ApplyCalOptions(correction=["rotationmeasure000"]),
        output_column=options.output_column,
    )
    add_history_entry(corrected_ms, HistoryEntry.IONOSPHERE_CORRECTED)

    return corrected_ms


def get_parser() -> ArgumentParser:
    """Create the argument parser for the ionosphere tasks."""
    parser = ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="task")

    download_parser = subparsers.add_parser(
        "download", help="Download the IONEX maps. Run where there is internet."
    )
    add_options_to_parser(parser=download_parser, options_class=IonosphereOptions)
    download_parser.add_argument("mss", type=Path, nargs="+", help="Measurement sets")

    correct_parser = subparsers.add_parser(
        "correct", help="Correct ionospheric Faraday rotation"
    )
    add_options_to_parser(parser=correct_parser, options_class=IonosphereOptions)
    correct_parser.add_argument("ms", type=Path, help="The measurement set")
    correct_parser.add_argument("--column", default="DATA", help="The data column")

    return parser


def cli() -> None:
    parser = get_parser()
    args = parser.parse_args()

    if args.task is None:
        parser.print_help()
        return

    options = create_options_from_parser(
        parser_namespace=args, options_class=IonosphereOptions
    )
    match args.task:
        case "download":
            download_ionex(args.mss, options)
        case "correct":
            correct_ionosphere(MS(path=args.ms, column=args.column), options)


if __name__ == "__main__":
    cli()
