"""Utilities to run the holly-roger sun flagging utilities"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Any

import astropy.units as u
import numpy as np
from jolly_roger.tractor import TukeyTractorOptions as JollyTukeyTractorOptions
from jolly_roger.tractor import tukey_tractor

from flint.logging import logger
from flint.options import (
    MS,
    BaseOptions,
    add_options_to_parser,
    create_options_from_parser,
)


class TukeyTractorOptions(BaseOptions):
    """Container for the Jolly-Roger tractor operations. These
    options map into the jolly_roger.tractor.TukeyTaperOptions
    class. Exception being that this container omits the usage
    of astropy quantities."""

    # Remember crew that astropy units do not always serialise nicely.
    outer_width: float = np.pi / 4
    """The start of the tapering in frequency space, in radians"""
    tukey_width: float = np.pi / 8
    """The width of the tapered region in frequency space, in radians"""
    data_column: str = "DATA"
    """The visibility column to modify"""
    output_column: str = "CORRECTED_DATA"
    """The output column to be created with the modified data"""
    copy_column_data: bool = False
    """Copy the data from the data column to the output column before applying the taper"""
    dry_run: bool = False
    """Indicates whether the data will be written back to the measurement set"""
    make_plots: bool = False
    """Create a small set of diagnostic plots. This can be slow."""
    overwrite: bool = False
    """If the output column exists it will be overwritten"""
    chunk_size: int = 1000
    """Size of the row-wise chunking iterator"""
    apply_towards_object: bool = True
    """apply the taper using the delay towards the target object. Otherwise delays away from zero are nulled with potential for significant smearing effects."""
    target_object: str = "Sun"
    """The target object to apply the delay towards."""
    elevation_cut_deg: float = -1
    """The elevation cut-off for the target object, in degrees. Defaults to -1."""
    ignore_nyquist_zone: int = 2
    """Do not apply the tukey taper if object is beyond this Nyquist zone"""


def _create_jolly_tractor_options(
    ms_path: Path, tukey_tractor_options: TukeyTractorOptions
) -> JollyTukeyTractorOptions:
    if not isinstance(tukey_tractor_options, TukeyTractorOptions):
        raise TypeError(f"Expect TukeyTaperOptions, got {type(tukey_tractor_options)}")

    logger.info("Creating jolly-roget TukeyTractorOptions object")
    return JollyTukeyTractorOptions(
        ms_path=ms_path,
        outer_width=tukey_tractor_options.outer_width,
        tukey_width=tukey_tractor_options.tukey_width,
        data_column=tukey_tractor_options.data_column,
        output_column=tukey_tractor_options.output_column,
        copy_column_data=tukey_tractor_options.copy_column_data,
        dry_run=tukey_tractor_options.dry_run,
        make_plots=tukey_tractor_options.make_plots,
        overwrite=tukey_tractor_options.overwrite,
        chunk_size=tukey_tractor_options.chunk_size,
        apply_towards_object=tukey_tractor_options.apply_towards_object,
        target_object=tukey_tractor_options.target_object,
        elevation_cut=tukey_tractor_options.elevation_cut_deg * u.deg,
        ignore_nyquist_zone=tukey_tractor_options.ignore_nyquist_zone,
    )


def jolly_roger_tractor(
    ms: MS | Path,
    tukey_tractor_options: TukeyTractorOptions | None = None,
    update_tukey_tractor_options: dict[str, Any] | None = None,
) -> tuple[MS, list[Path] | None]:
    """Run the `jolly_roger` tukey tractor method to null towards an object.

    Args:
        ms (MS | Path): The measurement set to modify
        tukey_tractor_options (TukeyTractorOptions | None, optional): Settings to modify the tukey tractor algorithm. If None defaults are used. Defaults to None.
        update_tukey_tractor_options (dict[str, Any] | None, optional): Over-rides the options of `tukey_tractor_options`. Defaults to None.

    Returns:
        tuple[MS, list[Path] | None]: Reference to modified measurement set. If the output
          plots were also created than these will be returned, None others.
    """
    ms = MS.cast(ms)
    if tukey_tractor_options is None:
        logger.info("Using default tukey tractor options")
        tukey_tractor_options = TukeyTractorOptions()

    assert isinstance(tukey_tractor_options, TukeyTractorOptions), (
        "Tukey taper is not correct type, which should not happen"
    )
    if update_tukey_tractor_options is not None:
        logger.info("Updating tukey tractor options")
        tukey_tractor_options = tukey_tractor_options.with_options(
            **update_tukey_tractor_options
        )

    jolly_tukey_tractor_options = _create_jolly_tractor_options(
        ms_path=ms.path, tukey_tractor_options=tukey_tractor_options
    )
    logger.info("Running the jolly-roger's tukey tractor")
    tukey_tractor_results = tukey_tractor(
        tukey_tractor_options=jolly_tukey_tractor_options
    )

    return ms.with_options(
        column=jolly_tukey_tractor_options.output_column
    ), tukey_tractor_results.output_plots


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Concise entry point to run jolly-roger")
    parser.add_argument(
        "ms_path", type=Path, help="Path to the measurement set to be modified"
    )

    parser = add_options_to_parser(parser=parser, options_class=TukeyTractorOptions)

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()
    tukey_tractor_options = create_options_from_parser(
        parser_namespace=args, options_class=TukeyTractorOptions
    )

    jolly_roger_tractor(ms=args.ms_path, tukey_tractor_options=tukey_tractor_options)


if __name__ == "__main__":
    cli()
