"""A basic interface into aegean source finding routines."""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Any, NamedTuple

from astropy.io import fits

from flint.exceptions import AttemptRerunException
from flint.logging import logger
from flint.naming import create_aegean_names
from flint.options import BaseOptions, add_options_to_parser, create_options_from_parser
from flint.sclient import run_singularity_command


class BANEOptions(BaseOptions):
    """Container for basic BANE related options. Only a subclass of BANE options are supported."""

    grid_size: tuple[int, int] | None = (16, 16)
    """The step interval of each box, in pixels"""
    box_size: tuple[int, int] | None = (196, 196)
    """The size of the box in pixels"""
    cores: int = 12
    """Number of cores to use. The number of stripes will be less than this number."""


class AegeanOptions(BaseOptions):
    """Container for basic aegean options. Only a subclass of aegean options are supported.

    Of note is the lack of a tables option (corresponding to --tables). This is dependent on knowing the base output name
    and relying on aegean to also append a suffix of sorts to the outputs. For that reason
    the aegean command generated will always create the table option.
    """

    nocov: bool = True
    """Whether aegean should attempt to model the co-variance of pixels. If true aegean does not. """
    maxsummits: int = 4
    """The maximum number of components an island is allowed to have before it is ignored. """
    autoload: bool = True
    """Attempt to load precomputed background and rms maps. """


class AegeanOutputs(NamedTuple):
    """Somple structure to represent output aegean products"""

    bkg: Path
    """Background map created by BANE"""
    rms: Path
    """RMS map created by BANE"""
    comp: Path
    """Source component catalogue created by Aegean"""
    beam_shape: tuple[float, float, float]
    """The `BMAJ`, `BMIN` and `BPA` that were stored in the image header that Aegen searched"""
    image: Path
    """The input image that was used to source find against"""


def _get_bane_command(image: Path, bane_options: BANEOptions) -> str:
    """Create the BANE command to run"""
    # The stripes is purposely set lower than the cores due to an outstanding bane bug that can cause a deadlock.
    cores = max(1, bane_options.cores)
    stripes = max(1, cores - 1)
    bane_command_str = f"BANE {image!s} --cores {cores} --stripes {stripes} "
    if bane_options.grid_size:
        bane_command_str += (
            f"--grid {bane_options.grid_size[0]} {bane_options.grid_size[1]} "
        )
    if bane_options.box_size:
        bane_command_str += (
            f"--box {bane_options.box_size[0]} {bane_options.box_size[1]}"
        )
    bane_command_str = bane_command_str.rstrip()
    logger.info("Constructed bane command.")

    return bane_command_str


def _bane_output_callback(line: str) -> None:
    """Callback handler for the BANE program. Will raise an error
    on the 'deadlock' issue."""

    assert isinstance(line, str)

    if "must be strictly ascending or descending" in line:
        logger.info("Potential BANE deadlock detectedc. Sleeping and raising error.")
        from time import sleep

        sleep(2)
        raise AttemptRerunException("BANE deadlock detected. ")


def _get_aegean_command(
    image: Path, base_output: str, aegean_options: AegeanOptions
) -> str:
    """Create the aegean command to run"""
    aegean_command = f"aegean {image!s} "
    if aegean_options.autoload:
        aegean_command += "--autoload "
    if aegean_options.nocov:
        aegean_command += "--nocov "

    # NOTE: Aegean will add the '_comp' component to the output tables. So, if we want
    # basename_comp.fits
    # to be the output component table then we want to pass
    # --table basename.fits
    # and have to rely on aegean doing the right thing.
    aegean_command += (
        f"--maxsummits {aegean_options.maxsummits} --table {base_output}.fits"
    )

    logger.info("Constructed aegean command. ")
    logger.debug(f"{aegean_command=}")

    return aegean_command


def run_bane_and_aegean(
    image: Path,
    aegean_container: Path,
    bane_options: BANEOptions | None = None,
    aegean_options: AegeanOptions | None = None,
    update_bane_options: dict[str, Any] | None = None,
    update_aegean_options: dict[str, Any] | None = None,
) -> AegeanOutputs:
    """Run BANE, the background and noise estimator, and aegean, the source finder,
    against an input image. This function attempts to hook into the AegeanTools
    module directly, which does not work with dask daemon processes.

    Args:
        image (Path): The input image that BANE will calculate a background and RMS map for
        aegean_container (Path): Path to a singularity container that was the AegeanTools packages installed.
        bane_options (Optional[BANEOptions], optional): The options that are provided to BANE. If None defaults of BANEOptions are used. Defaults to None.
        aegean_options (Optional[AegeanOptions], optional): The options that are provided to Aegean. if None defaults of AegeanOptions are used. Defaults to None.
        update_bane_options (dict[str, Any] | None, optional): Over-ride any default options of BANEOptions. If None defaults are used. Defaults to None.
        update_aegean_options (dict[str, Any] | None, optional): Over-ride any default options of AegeanOptions. If None defaults are used. Defaults to None.

    Returns:
        AegeanOutputs: The newly created BANE products
    """
    bane_options = bane_options if bane_options else BANEOptions()
    if update_bane_options:
        bane_options = bane_options.with_options(**update_bane_options)

    aegean_options = aegean_options if aegean_options else AegeanOptions()
    if update_aegean_options:
        aegean_options = aegean_options.with_options(**update_aegean_options)

    image = image.absolute()
    base_output = str(image.parent / image.stem)
    logger.info(f"Using base output name of: {base_output}")

    aegean_names = create_aegean_names(base_output=base_output)
    logger.debug(f"{aegean_names=}")

    bane_command_str = _get_bane_command(image=image, bane_options=bane_options)

    bind_dir = [image.absolute().parent]
    run_singularity_command(
        image=aegean_container,
        command=bane_command_str,
        stream_callback_func=_bane_output_callback,
        bind_dirs=bind_dir,
    )

    aegean_command = _get_aegean_command(
        image=image, base_output=base_output, aegean_options=aegean_options
    )
    run_singularity_command(
        image=aegean_container, command=aegean_command, bind_dirs=bind_dir
    )

    # These are the bane outputs
    bkg_image_path = aegean_names.bkg_image
    rms_image_path = aegean_names.rms_image

    image_header = fits.getheader(image)
    image_beam = (
        image_header["BMAJ"],
        image_header["BMIN"],
        image_header["BPA"],
    )

    aegean_outputs = AegeanOutputs(
        bkg=bkg_image_path,
        rms=rms_image_path,
        comp=aegean_names.comp_cat,
        beam_shape=image_beam,
        image=image,
    )

    logger.info(f"Aegeam finished running. {aegean_outputs=}")

    return aegean_outputs


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode")

    bane_parser = subparsers.add_parser(
        name="find", help="Run BANE with default options. "
    )

    bane_parser.add_argument(
        "image", type=Path, help="The image that BANE will process"
    )
    bane_parser.add_argument(
        "container", type=Path, help="Path to container with AegeanTools"
    )
    bane_parser = add_options_to_parser(parser=bane_parser, options_class=BANEOptions)
    bane_parser = add_options_to_parser(parser=bane_parser, options_class=AegeanOptions)

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    if args.mode == "find":
        bane_options = create_options_from_parser(
            parser_namespace=args, options_class=BANEOptions
        )
        aegean_options = create_options_from_parser(
            parser_namespace=args, options_class=AegeanOptions
        )
        run_bane_and_aegean(
            image=args.image,
            aegean_container=args.container,
            bane_options=bane_options,
            aegean_options=aegean_options,
        )
    else:
        logger.info(f"Mode '{args.mode}' is not known.")
        parser.print_help()


if __name__ == "__main__":
    cli()
