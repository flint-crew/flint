"""Utilities to run the holly-roger sun flagging utilities"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from capn_crunch import add_options_to_parser, create_options_from_parser
from jolly_roger.tractor import TukeyTractorOptions, tukey_tractor

from flint.logging import logger
from flint.options import (
    MS,
)


def jolly_roger_tractor(
    ms: MS | Path,
    tukey_tractor_options: TukeyTractorOptions | None = None,
    update_tukey_tractor_options: dict[str, Any] | None = None,
) -> MS:
    """Run the `jolly_roger` tukey tractor method to null towards an object.

    Args:
        ms (MS | Path): The measurement set to modify
        tukey_tractor_options (TukeyTractorOptions | None, optional): Settings to modify the tukey tractor algorithm. If None defaults are used. Defaults to None.
        update_tukey_tractor_options (dict[str, Any] | None, optional): Over-rides the options of `tukey_tractor_options`. Defaults to None.

    Returns:
        MS: Reference to modified measurement set.
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

    logger.info("Running the jolly-roger's tukey tractor")
    tukey_tractor(ms_path=ms.path, tukey_tractor_options=tukey_tractor_options)

    return ms.with_options(column=tukey_tractor_options.output_column)


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
