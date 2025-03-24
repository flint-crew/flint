"""Tooling around the use of the `addmodel` program packaged with `calibrate`.

This tooling requires the `calibrate` container.
"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Literal

from flint.logging import logger
from flint.ms import remove_columns_from_ms
from flint.options import BaseOptions, add_options_to_parser, create_options_from_parser
from flint.sclient import run_singularity_command


class AddModelOptions(BaseOptions):
    """Container for options into the ``addmodel`` program packaged
    with ``aocalibrate``"""

    model_path: Path
    """Path to the sky-model that will be inserted"""
    ms_path: Path
    """Path to the measurement set that will be interacted with"""
    mode: Literal["a", "s", "c", "v"]
    """The mode ``addmodel`` will be operating under, where where a=add model to visibilities (default), s=subtract model from visibilities, c=copy model to visibilities, z=zero visibilities"""
    datacolumn: str
    """The column that will be operated against"""


def add_model_options_to_command(add_model_options: AddModelOptions) -> str:
    """Generate the command to execute ``addmodel``

    Args:
        add_model_options (AddModelOptions): The collection of supported options used to generate the command

    Returns:
        str: The generated addmodel command
    """
    logger.info("Generating addmodel command")
    command = f"addmodel -datacolumn {add_model_options.datacolumn} -m {add_model_options.mode} "
    command += f"{add_model_options.model_path!s} {add_model_options.ms_path!s}"

    return command


def add_model(
    add_model_options: AddModelOptions, container: Path, remove_datacolumn: bool = False
) -> AddModelOptions:
    """Use the ``addmodel`` program to predict the sky-model visibilities
    from a compatible source list (e.g. ``wsclean -save-source-list``)

    Args:
        add_model_options (AddModelOptions): The set of supported options to be supplied to ``addmodel``
        container (Path): The calibrate container that contains the ``addmodel`` program
        remove_datacolumn (bool, optional): Whether to first remove the ``datacolumn`` specified in ``add_model_options`` before predicting. If False it should be overwritten. Defaults to False.

    Returns:
        AddModelOptions: The options used to run ``addmodel`` (same as input)
    """
    if remove_datacolumn:
        remove_columns_from_ms(
            ms=add_model_options.ms_path, columns_to_remove=add_model_options.datacolumn
        )
    add_model_command = add_model_options_to_command(
        add_model_options=add_model_options
    )

    run_singularity_command(
        image=container,
        command=add_model_command,
        bind_dirs=[add_model_options.ms_path, add_model_options.model_path],
    )

    return add_model_options


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="Predict a set of model visibilities from a model with addmodel"
    )

    parser.add_argument(
        "calibrate_container", type=Path, help="Path to the container with addmodel"
    )
    parser.add_argument(
        "--remove-datacolumn",
        action="store_true",
        help="Remove the column being predicted into before predicting. See the AddModelOptions.datacolumn option.",
    )

    parser = add_options_to_parser(parser=parser, options_class=AddModelOptions)

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    add_model_options = create_options_from_parser(
        parser_namespace=args, options_class=AddModelOptions
    )

    add_model(
        add_model_options=add_model_options,
        container=args.calibrate_container,
        remove_datacolumn=args.remove_datacolumn,
    )


if __name__ == "__main__":
    cli()
