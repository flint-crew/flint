"""Items in and out the model visibility prediction using the `crystalball` python package."""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from crystalball.crystalball import predict
from dask.distributed import Client

from flint.imager.wsclean import get_wsclean_output_source_list_path
from flint.logging import logger
from flint.ms import MS
from flint.options import BaseOptions, add_options_to_parser, create_options_from_parser


class CrystalBallOptions(BaseOptions):
    """Options related to running crystal ball"""

    crystallball_wsclean_pol_mode: list[str] = ["i"]
    """The polarisation of the wsclean model that was generated"""
    row_chunks: int = 0
    "Number of rows of input MS that are processed in a single chunk. If 0 it will be set automatically. Default is 0."
    model_chunks: int = 0
    "Number of sky model components that are processed in a single chunk. If 0 it will be set automatically. Default is 0."


def crystalball_predict(
    ms: MS,
    crystalball_options: CrystalBallOptions,
    wsclean_source_list_path: Path | None = None,
    dask_client: Client | None = None,
    output_column: str = "MODEL_DATA",
    update_crystalball_options: dict[str, Any] | None = None,
) -> MS:
    """A very simply wrapper around the `Crystalball.predict` function. Basic
    checks to ensure that the BB6 style source model path exists, which is the
    format used by the `wsclean -save-source-list` option.

    If no `wsclean_source_list_path` is specified one is guess from the name of the
    input MS.path.

    Args:
        ms (MS): The MS instance whose path is to the measurement set to predict into.
        crystalball_options (CrystalBallOptions): Options that control the `crystalball.predict` call.
        wsclean_source_list_path (Path | None, optional): The path to the file with the model components to predict. If None an attempt is made to find it from the MS. Defaults to None.
        dask_client (Client | None, optional): A specialised Dask distributed task. If None one is created by `crystalball`. Defaults to None.
        output_column (str, optional): The column to predict into. The `MS.model_column` will reflect this. Defaults to "MODEL_DATA".
        update_crystalball_options (dict[str, Any] | None, optional): Update options to the provided crystalball_options. Defaults to None.

    Returns:
        MS: The MS that was predicted into, with the `model_column` set appropriately.
    """
    if update_crystalball_options:
        crystalball_options = crystalball_options.with_options(
            **update_crystalball_options
        )

    if wsclean_source_list_path is None:
        assert len(crystalball_options.crystallball_wsclean_pol_mode) == 1, (
            "Only a single polarisation mode is currently supported."
        )

        pol = crystalball_options.crystallball_wsclean_pol_mode[0]
        logger.info(f"Using {pol=}")
        wsclean_source_list_path = get_wsclean_output_source_list_path(
            name_path=ms.path, pol=pol
        )

    assert isinstance(wsclean_source_list_path, Path), (
        f"{wsclean_source_list_path=}, which appears not to be a Path"
    )
    if not wsclean_source_list_path.exists():
        message = f"{wsclean_source_list_path=} was requested, but does not exist"
        raise FileNotFoundError(message)

    logger.info(f"Adding {wsclean_source_list_path=} to {ms.path=}")
    logger.info(f"Predicting into {output_column=} with Crystalball")

    if dask_client:
        logger.info(f"Using {dask_client=}")

    predict(
        ms=str(ms.path),
        sky_model=str(wsclean_source_list_path),
        output_column=output_column,
        client=dask_client,
        row_chunks=crystalball_options.row_chunks,
        model_chunks=crystalball_options.model_chunks,
    )

    return ms.with_options(model_column="MODEL_DATA")


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
    parser.add_argument(
        "--wsclean-source-list-path",
        type=Path,
        default=None,
        help="Path to the model to load. If None one is guessed from the MS name.",
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

    crystalball_predict(
        ms=ms,
        crystalball_options=crystalball_options,
        wsclean_source_list_path=args.wsclean_source_list_path,
    )


if __name__ == "__main__":
    cli()
