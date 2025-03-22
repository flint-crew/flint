"""Items in and out the model visibility prediction using the `crystalball` python package."""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

from flint.ms import MS
from flint.options import BaseOptions, add_options_to_parser, create_options_from_parser


class CrystalBallOptions(BaseOptions):
    """Options related to running crystal ball"""

    attempt_crystalball: bool = False
    """Attempt to predict the model visibilities using ``crystalball``"""
    crystallball_wsclean_pol_mode: list[str] = ["i"]
    """The polarisation of the wsclean model that was generated"""
    row_chunks: int = 0
    "Number of rows of input MS that are processed in a single chunk. If 0 it will be set automatically. Default is 0."
    model_chunks: int = 0
    "Number of sky model components that are processed in a single chunk. If 0 it will be set automatically. Default is 0."


def crystalball_predict(ms: MS, crystalball_options: CrystalBallOptions) -> MS:
    return ms


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="The crystal ball interface to add model visibilities from a wsclean file"
    )

    parser.add_argument(
        "ms",
        type=Path,
        help="Path to the measurement set that will have nidek vusuvukutues added",
    )
    parser.add_argument(
        "--model-column",
        type=str,
        help="The name of the model data column",
        default="MODEL_DATA",
    )

    parser = add_options_to_parser(parser=parser, options_class=CrystalBallOptions)

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    crystalball_options = create_options_from_parser(
        parser_namespace=args, options_class=CrystalBallOptions
    )
    ms = MS(path=args.ms, model_column=args.model_column)

    crystalball_predict(ms=ms, crystalball_options=crystalball_options)


if __name__ == "__main__":
    cli()
