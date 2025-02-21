"""Tooling related to the convolution of images. Principally
this is mostly to smooth to a common resolution
"""

from __future__ import annotations

import warnings
from argparse import ArgumentParser
from pathlib import Path
from shutil import copyfile
from typing import Collection, Literal, NamedTuple

import astropy.units as u
import numpy as np
from astropy.io import fits
from astropy.wcs import FITSFixedWarning
from racs_tools import beamcon_2D, beamcon_3D
from radio_beam import Beam, Beams

from flint.logging import logger

warnings.simplefilter("ignore", FITSFixedWarning)


class BeamShape(NamedTuple):
    """A simple container to represent a fitted 2D gaussian,
    intended for the main lobe of the synthesised beam. This
    class has been defined to avoid issues with the serialisation
    of astropy.units, which can cause strange and wonderful
    errors when being sent over the wire to workers."""

    bmaj_arcsec: float
    """The size of the major-axis of the beam, in arcseconds."""
    bmin_arcsec: float
    """The size of the minor-axis of the beam, in arcseconds."""
    bpa_deg: float
    """Rotation of the beam, in degrees."""

    @classmethod
    def from_radio_beam(cls, radio_beam: Beam) -> BeamShape:
        """A helper function to convert a radio_beam.Beam into a
        BeamShape. This is prinicpally intended to be used when
        there is a need to exchange a Beam between processes
        that would need to serialise the object.

        Args:
            radio_beam (Beam): The Beam to convert to normalised and known units

        Returns:
            BeamShape: The normalised container without astropy units.
        """
        return cls(
            bmaj_arcsec=radio_beam.major.to(u.arcsecond).value,  # type: ignore
            bmin_arcsec=radio_beam.minor.to(u.arcsecond).value,  # type: ignore
            bpa_deg=radio_beam.pa.to(u.degree).value,  # type: ignore
        )


def check_if_cube_fits(fits_file: Path) -> bool:
    """Check to see whether the data component of a FITS images is a cube.
    Returns ``True`` is the data-shape needs 3-dimensions to be represented.

    Note: Unclear on usefulness

    Args:
        fits_file (Path): FITS file that will be examinined

    Returns:
        bool: Whether the input FITS file is a cube or not.
    """

    try:
        squeeze_data = np.squeeze(fits.getdata(fits_file))  # type: ignore
    except FileNotFoundError:
        return False
    except OSError:
        return False

    return len(squeeze_data.shape) == 3


def get_cube_common_beam(
    cube_paths: Collection[Path], cutoff: float | None = None
) -> list[BeamShape]:
    """Given a set of input cube FITS files, compute a common beam
    for each channel.

    Args:
        cube_paths (Collection[Path]): Set of cube FITS files to inspect to derive a common beam
        cutoff (Optional[float], optional): A cutoff value, in arcsec, that specifies the maximum BMAJ allowed. Defaults to None.

    Returns:
        List[BeamShape]: List of target beam shapes to use, corresponding to each channel
    """

    _, common_beam_data_list = beamcon_3D.smooth_fits_cube(
        infiles_list=list(cube_paths),
        dryrun=True,
        cutoff=cutoff,
        mode="natural",
        conv_mode="robust",
        ncores=1,
    )
    # Make proper check here that accounts for NaNs
    for file in common_beam_data_list:
        assert all(
            (file[0].major == common_beam_data_list[0][0].major)
            | np.isnan(file[0].major)
        )
        assert all(
            (file[0].minor == common_beam_data_list[0][0].minor)
            | np.isnan(file[0].minor)
        )
        assert all(
            (file[0].pa == common_beam_data_list[0][0].pa) | np.isnan(file[0].pa)
        )

    first_cube_fits_beam = common_beam_data_list[0][0]
    assert isinstance(first_cube_fits_beam, Beams), (
        f"Unexpected type for common beams. Expected Beams, got {type(first_cube_fits_beam)}"
    )

    beam_shape_list = [
        BeamShape.from_radio_beam(radio_beam=beam)  # type: ignore
        for beam in first_cube_fits_beam
    ]
    return beam_shape_list


def convolve_cubes(
    cube_paths: Collection[Path],
    beam_shapes: list[BeamShape],
    cutoff: float | None = None,
    convol_suffix: str = "conv",
    executor_type: Literal["thread", "process", "mpi"] = "thread",
) -> Collection[Path]:
    logger.info(f"Will attempt to convol {len(cube_paths)} cubes")
    if cutoff:
        logger.info(f"Supplied cutoff {cutoff}")

    # Extractubg the beam properties
    beam_major_list = [float(beam.bmaj_arcsec) for beam in beam_shapes]
    beam_minor_list = [float(beam.bmin_arcsec) for beam in beam_shapes]
    beam_pa_list = [float(beam.bpa_deg) for beam in beam_shapes]

    # Sanity test
    assert len(beam_major_list) == len(beam_minor_list) == len(beam_pa_list)

    logger.info("Convoling cubes")
    cube_data_list, _, _ = beamcon_3D.smooth_fits_cube(
        infiles_list=list(cube_paths),
        dryrun=False,
        cutoff=cutoff,
        mode="natural",
        conv_mode="robust",
        bmaj=beam_major_list,
        bmin=beam_minor_list,
        bpa=beam_pa_list,
        suffix=convol_suffix,
        executor_type=executor_type,
    )

    # Construct the name of the new file created. For the moment this is done
    # manually as it is not part of the returned object
    # TODO: Extend the return struct from beamcon_3D to include output name
    convol_cubes_path = [
        Path(cube_data.filename).with_suffix(f".{convol_suffix}.fits")
        for cube_data in cube_data_list
    ]

    # Show the mapping as a sanity check
    for input_cube, output_cube in zip(list(cube_paths), convol_cubes_path):
        logger.info(f"{input_cube=} convolved to {output_cube}")

    # Trust no one
    assert all([p.exists() for p in convol_cubes_path]), (
        "A convolved cube does not exist"
    )
    return convol_cubes_path


def get_common_beam(
    image_paths: Collection[Path], cutoff: float | None = None
) -> BeamShape:
    """Return the minimum beam size required to encompass the beams described
    in the FITS header (e.g. BMAJ,BMIN,BPA) of the input images. This is used
    when preparing to convolve to a common resolution.

    Args:
        image_paths (Collection[Path]): The paths to the FITS images that will be examined.
        cutoff (Optional[float], optional): The maximum beam size an input image is allowed to have. BMAJ's larger than this are ignored from the calculation. Defaults to None.

    Returns:
        BeamShape: Smalled common beam available to be used
    """

    logger.info(f"Calculating common beam size of {len(image_paths)} images. ")

    if cutoff:
        logger.info(f"Setting beam cutoff to {cutoff} arcseconds. ")

    try:
        beam, beams = beamcon_2D.get_common_beam(files=list(image_paths), cutoff=cutoff)

        beam_shape = BeamShape.from_radio_beam(beam)
        logger.info(f"Constructed {beam_shape=}")
    except ValueError:
        logger.info("The beam was not constrained. Setting to NaNs")
        beam_shape = BeamShape(bmaj_arcsec=np.nan, bmin_arcsec=np.nan, bpa_deg=np.nan)

    return beam_shape


def convolve_images(
    image_paths: Collection[Path],
    beam_shape: BeamShape,
    cutoff: float | None = None,
    convol_suffix: str = "conv",
    output_paths: list[Path] | None = None,
) -> list[Path]:
    """Convolve a set of input images to a common resolution as specified
    by the beam_shape. If the major-axis of the native resolution is larger
    than cutoff (in arcseconds) then the racs_tools beamconv_2D task will
    nan it.

    Additionally, some input subject image will simply copied if:

    * the input ``beam_shape`` is not finite, or
    * the beamshape encoded in the FITS header of the subject image is not defined

    Args:
        image_paths (Collection[Path]): Set of image paths to FITS images to convol
        beam_shape (BeamShape): The specification of the desired final resolution
        cutoff (Optional[float], optional): Images whose major-axis is larger than this will be blank. Expected in arcseconds. Defaults to None.
        convol_suffix (str, optional): The suffix added to .fits to indicate smoothed image. Defaults to 'conv'.
        output_paths (list[Path] | None, optional): The final output file namesfor each input image. If provided this renamed files created using the `convol_suffix`. Defaults to None.

    Returns:
        Collection[Path]: Set of paths to the smoothed images
    """

    logger.info(f"Will attempt to convolve {len(image_paths)} images.")
    if cutoff:
        logger.info(f"Supplied cutoff of {cutoff} arcsecond")

    if not np.isfinite(beam_shape.bmaj_arcsec):
        logger.info("Beam shape is not defined. Copying files into place. ")

        conv_image_paths = [
            Path(str(image_path).replace(".fits", f".{convol_suffix}.fits"))
            for image_path in image_paths
        ]
        # If the beam is not defined, simply copy the file into place. Although
        # this takes up more space, it is not more than otherwise
        for original_path, copy_path in zip(image_paths, conv_image_paths):
            logger.info(f"Copying {original_path=} {copy_path=}")
            copyfile(original_path, copy_path)

        return conv_image_paths

    radio_beam = Beam(
        major=beam_shape.bmaj_arcsec * u.arcsecond,
        minor=beam_shape.bmin_arcsec * u.arcsecond,
        pa=beam_shape.bpa_deg * u.deg,
    )

    return_conv_image_paths: list[Path] = []

    if output_paths:
        assert isinstance(output_paths, type(image_paths)), (
            "Types for image_paths and output_paths need to be the same"
        )
        assert len(output_paths) == len(image_paths), (
            f"Mismatch collection lengths of image_paths ({len(image_paths)}) and output_paths ({len(output_paths)})"
        )

    for idx, image_path in enumerate(image_paths):
        convol_output_path: Path = Path(
            str(image_path).replace(".fits", f".{convol_suffix}.fits")
        )
        header = fits.getheader(image_path)
        if header["BMAJ"] == 0.0:
            logger.info(f"Copying {image_path} to {convol_output_path=} for empty beam")
            copyfile(image_path, convol_output_path)
        else:
            logger.info(f"Convolving {image_path.name!s}")
            beamcon_2D.beamcon_2d_on_fits(
                file=image_path,
                outdir=None,
                new_beam=radio_beam,
                conv_mode="robust",
                suffix=convol_suffix,
                cutoff=cutoff,
            )

        if output_paths:
            output_path: Path = output_paths[idx]
            logger.info(f"Renaming generate convolved file to {output_path=}")
            convol_output_path.rename(output_path)
            convol_output_path = output_path

            # Pirates trust nothing, especially with the silly logic
            assert convol_output_path.exists(), (
                f"{convol_output_path=} should exist, but doesn't"
            )

        return_conv_image_paths.append(convol_output_path)

    return return_conv_image_paths


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)

    subparsers = parser.add_subparsers(dest="mode")

    convol_parser = subparsers.add_parser(
        "convol", help="Convol images to a common resolution"
    )

    convol_parser.add_argument(
        "images",
        type=Path,
        nargs="+",
        help="The images that will be convolved to a common resolution",
    )
    convol_parser.add_argument(
        "--cutoff",
        type=float,
        default=None,
        help="Beams whose major-axis are larger then this (in arcseconds) are ignored from the calculation of the optimal beam.",
    )
    convol_parser.add_argument(
        "--convol-suffix",
        type=str,
        default="conv",
        help="The suffix added to convolved images. ",
    )
    convol_parser.add_argument(
        "--cubes",
        action="store_true",
        default=False,
        help="Treat the input files as cubes and use the corresponding 3D beam selection and convolution. ",
    )

    maxbeam_parser = subparsers.add_parser(
        "maxbeam", help="Find the optimal beam size for a set of images."
    )

    maxbeam_parser.add_argument(
        "images",
        type=Path,
        nargs="+",
        help="The images that will be convolved to a common resolution",
    )
    maxbeam_parser.add_argument(
        "--cutoff",
        type=float,
        default=None,
        help="Beams whose major-axis are larger then this (in arcseconds) are ignored from the calculation of the optimal beam.",
    )

    cubemaxbeams_parser = subparsers.add_parser(
        "cubemaxbeam",
        help="Calculate the set of common beams across channels in a set of cubes",
    )
    cubemaxbeams_parser.add_argument(
        "cubes",
        type=Path,
        nargs="+",
        help="The images that will be convolved to a common resolution",
    )
    cubemaxbeams_parser.add_argument(
        "--cutoff",
        type=float,
        default=None,
        help="Beams whose major-axis are larger then this (in arcseconds) are ignored from the calculation of the optimal beam.",
    )

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    if args.mode == "maxbeam":
        get_common_beam(image_paths=args.images, cutoff=args.cutoff)
    if args.mode == "convol":
        if args.cubes:
            assert all([check_if_cube_fits(fits_file=f) for f in args.images]), (
                "Not all input files are FITS cubes"
            )
            common_beams = get_cube_common_beam(
                cube_paths=args.images, cutoff=args.cutoff
            )
            for image in args.images:
                logger.info(f"Convoling {image}")
                _ = convolve_cubes(
                    cube_paths=[image],
                    beam_shapes=common_beams,
                    cutoff=args.cutoff,
                    convol_suffix=args.convol_suffix,
                )

        else:
            assert not all([check_if_cube_fits(fits_file=f) for f in args.images]), (
                "Not all input files are FITS images (not cubes)"
            )
            common_beam = get_common_beam(image_paths=args.images, cutoff=args.cutoff)
            _ = convolve_images(
                image_paths=args.images,
                beam_shape=common_beam,
                cutoff=args.cutoff,
                convol_suffix=args.convol_suffix,
            )
    if args.mode == "cubemaxbeam":
        common_beam_shape_list = get_cube_common_beam(
            cube_paths=args.cubes, cutoff=args.cutoff
        )
        logger.info(f"Extracted {common_beam_shape_list=}")


if __name__ == "__main__":
    cli()
