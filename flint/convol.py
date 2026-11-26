"""Tooling related to the convolution of images. Principally
this is mostly to smooth to a common resolution
"""

from __future__ import annotations

import warnings
from argparse import ArgumentParser
from collections.abc import Collection, Sequence
from pathlib import Path
from shutil import copyfile
from typing import Any, Literal, NamedTuple

import astropy.units as u
import convolve_rs
import numpy as np
from astropy.io import fits
from astropy.wcs import FITSFixedWarning
from radio_beam import Beam, Beams
from radio_beam.beam import NoBeamException
from radio_beam.utils import BeamError

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
    def from_convolve_rs(cls, beam: convolve_rs.Beam | None) -> BeamShape:
        """A convolve-rs beam, or NaNs where there is none."""
        if beam is None:
            return cls(bmaj_arcsec=np.nan, bmin_arcsec=np.nan, bpa_deg=np.nan)
        return cls(
            bmaj_arcsec=beam.major_arcsec,
            bmin_arcsec=beam.minor_arcsec,
            bpa_deg=beam.pa_deg,
        )

    def to_convolve_rs(self) -> convolve_rs.Beam | None:
        """This beam for convolve-rs, or None if it is not finite."""
        if not np.isfinite(self).all():
            return None
        return convolve_rs.Beam.from_arcsec(
            self.bmaj_arcsec, self.bmin_arcsec, self.bpa_deg
        )

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


def _beams_from_cubes(cube_paths: list[Path]) -> Beams:
    """Every channel beam of every cube, as one Beams. A masked channel gets a zero beam."""
    beams = [
        beam for cube_path in cube_paths for beam in convolve_rs.cube_beams(cube_path)
    ]
    return Beams(
        major=[0.0 if b is None else b.major_arcsec for b in beams] * u.arcsec,
        minor=[0.0 if b is None else b.minor_arcsec for b in beams] * u.arcsec,
        pa=[0.0 if b is None else b.pa_deg for b in beams] * u.deg,
    )


def usable_beam_mask(beams: Beams, cutoff: float | None = None) -> np.ndarray:
    """Which beams describe a real PSF that may be brought to a common resolution.

    Blank channels carry a placeholder beam rather than a real one, and a
    channel coarser than ``cutoff`` is to be blanked rather than convolved to.
    Neither constrains a common beam, and neither may be handed to
    ``radio_beam``, whose common-beam solver reduces a set with no usable beam
    in it to an empty sequence and raises an opaque ``argmax`` error.

    Args:
        beams (Beams): The beams to inspect
        cutoff (float | None, optional): Beams whose major axis exceeds this, in arcsec, are not usable. Defaults to no cutoff.

    Returns:
        np.ndarray: Boolean mask, True where the beam is usable
    """
    major = np.asarray(beams.major.to(u.arcsecond).value, dtype=float)
    minor = np.asarray(beams.minor.to(u.arcsecond).value, dtype=float)
    pa = np.asarray(beams.pa.to(u.degree).value, dtype=float)

    # A blank channel is marked with an exactly-zero beam (wsclean's convention
    # for a plane with no fitted PSF) or, in a BEAMS table, with the positive
    # sentinel `fitscube` writes there because CASA rejects a NaN. `> 0` alone
    # would let the latter through and have a cube already at one beam
    # reconvolved for the sake of its blank channels.
    blank_beam_arcsec = float(np.finfo(np.float32).tiny)

    usable = (
        (major > blank_beam_arcsec)
        & (minor > blank_beam_arcsec)
        & np.isfinite(major)
        & np.isfinite(minor)
        & np.isfinite(pa)
    )
    if cutoff is not None:
        usable &= major <= cutoff

    return usable


def beam_from_header(header: fits.Header | dict[str, Any]) -> Beam | None:
    """The restoring beam recorded in a FITS header, or None when there is none"""
    try:
        return Beam.from_fits_header(header)
    except (NoBeamException, KeyError):
        return None


def header_beam_is_usable(
    header: fits.Header | dict[str, Any], cutoff: float | None = None
) -> bool:
    """Whether the beam a FITS header records is a real PSF. See ``usable_beam_mask``"""
    beam = beam_from_header(header=header)
    if beam is None:
        return False

    return bool(usable_beam_mask(beams=Beams(beams=[beam]), cutoff=cutoff)[0])


def _cube_beams(cube_paths: Collection[Path]) -> Beams | None:
    """Every channel beam of every cube, or None when there is no beam to read"""
    try:
        return _beams_from_cubes(cube_paths=list(cube_paths))
    except ValueError:
        logger.info(f"No beam information found among {list(cube_paths)=}")
        return None


def cubes_share_common_beam(
    cube_paths: Collection[Path], cutoff: float | None = None
) -> bool:
    """Whether a single restoring beam already describes every channel of every
    cube. Placeholder beams are ignored: a blanked channel holds no signal and
    so has no resolution to make common. Cubes with no beam information at all,
    or with nothing but placeholders, likewise have nothing to bring together.

    A channel whose beam is coarser than ``cutoff`` is one to blank, which only
    a convolution pass will do, so such a cube does not share a common beam.

    Args:
        cube_paths (Collection[Path]): The FITS cubes to inspect
        cutoff (float | None, optional): Channels coarser than this, in arcsec, are to be blanked rather than convolved to. Defaults to no cutoff.

    Returns:
        bool: Whether the cubes are already at a common resolution
    """
    beams = _cube_beams(cube_paths=cube_paths)
    if beams is None:
        return True

    usable = usable_beam_mask(beams=beams, cutoff=cutoff)
    if not usable.any():
        logger.info(f"No usable restoring beam among {list(cube_paths)=}")
        return True

    # A real beam that the cutoff excludes is a channel to blank, which is work
    # only the convolution pass does
    if cutoff is not None and (usable_beam_mask(beams=beams) & ~usable).any():
        return False

    usable_beams = beams[usable]
    return all(beam == usable_beams[0] for beam in usable_beams)


def common_beam_from_beams(beams: Beams, cutoff: float | None = None) -> Beam | None:
    """The smallest beam every usable beam fits inside, or None when none is usable.
    See ``usable_beam_mask``."""
    usable = usable_beam_mask(beams=beams, cutoff=cutoff)
    if not usable.any():
        return None

    logger.info(f"Deriving a common beam from {usable.sum()} of {len(usable)} beams")
    usable_beams = beams[usable]
    try:
        common_beam = usable_beams.common_beam()
    except BeamError:
        logger.warning(
            "Could not find a common beam with the default tolerance, trying again"
        )
        common_beam = usable_beams.common_beam(tolerance=1e-5)

    # The minimum enclosing ellipse radio_beam solves for sits right against the
    # beams it encloses, so a channel whose own beam it barely covers can fail to
    # deconvolve. Rounding up gives every channel the headroom to reach it, as
    # the common beams racs_tools and convolve-rs derive themselves are.
    rounded = convolve_rs.round_common_beam(
        BeamShape.from_radio_beam(common_beam).to_convolve_rs(),
        [BeamShape.from_radio_beam(b).to_convolve_rs() for b in usable_beams],
    )
    return Beam(
        major=rounded.major_arcsec * u.arcsecond,
        minor=rounded.minor_arcsec * u.arcsecond,
        pa=rounded.pa_deg * u.degree,
    )


def common_beam_from_beam_shapes(
    beam_shapes: Collection[BeamShape],
    cutoff: float | None = None,
    fixed_beam_shape: Sequence[float] | None = None,
) -> BeamShape:
    """The smallest beam every usable beam fits inside, or NaNs when none is usable.

    Args:
        beam_shapes (Collection[BeamShape]): The beams to cover
        cutoff (float | None, optional): Beams whose major axis exceeds this, in arcsec, are left out rather than dragging the common beam out to them. Defaults to no cutoff.
        fixed_beam_shape (Sequence[float] | None, optional): Return this (arcsec, arcsec, deg) rather than solving one. Defaults to solving.

    Returns:
        BeamShape: The common beam
    """
    if fixed_beam_shape:
        return BeamShape(*fixed_beam_shape)

    logger.info(f"Solving a common beam over {len(beam_shapes)} beams")
    major, minor, pa = np.array(beam_shapes, dtype=float).reshape(-1, 3).T
    common_beam = common_beam_from_beams(
        beams=Beams(major=major * u.arcsec, minor=minor * u.arcsec, pa=pa * u.deg),
        cutoff=cutoff,
    )
    if common_beam is None:
        return BeamShape(bmaj_arcsec=np.nan, bmin_arcsec=np.nan, bpa_deg=np.nan)

    return BeamShape.from_radio_beam(radio_beam=common_beam)


def common_beam_from_cubes(
    cube_paths: Collection[Path], cutoff: float | None = None
) -> Beam | None:
    """The smallest beam that encompasses every usable channel of every cube.

    Args:
        cube_paths (Collection[Path]): The FITS cubes to inspect
        cutoff (float | None, optional): Channels coarser than this, in arcsec, are left out of the common beam rather than dragging every channel out to their resolution. Defaults to no cutoff.

    Returns:
        Beam | None: The beam every usable channel fits inside, or None when no channel of any cube carries a real restoring beam
    """
    beams = _cube_beams(cube_paths=cube_paths)
    if beams is None:
        return None

    common_beam = common_beam_from_beams(beams=beams, cutoff=cutoff)
    if common_beam is None:
        logger.warning(
            f"No usable restoring beam among {list(cube_paths)=}, so no common beam"
        )
    return common_beam


def common_beam_shape_from_cubes(
    cube_paths: Collection[Path], cutoff: float | None = None
) -> BeamShape:
    """The common beam of a set of cubes, for a caller that cannot do without
    one. See ``common_beam_from_cubes``.

    Args:
        cube_paths (Collection[Path]): The FITS cubes to inspect
        cutoff (float | None, optional): Channels coarser than this, in arcsec, are left out of the common beam. Defaults to no cutoff.

    Raises:
        ValueError: If no channel of any cube carries a real restoring beam

    Returns:
        BeamShape: The beam every usable channel fits inside
    """
    common_beam = common_beam_from_cubes(cube_paths=cube_paths, cutoff=cutoff)
    if common_beam is None:
        msg = f"No usable restoring beam among {list(cube_paths)=}"
        raise ValueError(msg)

    return BeamShape.from_radio_beam(radio_beam=common_beam)


def convolve_plane_to_beam(
    plane: Path,
    beam_shape: BeamShape,
    cutoff: float | None = None,
    convol_suffix: str = "conv",
) -> Path:
    """Convolve a single channel plane to ``beam_shape``, writing a new image
    alongside it and leaving the input in place.

    A plane coarser than ``cutoff`` is blanked rather than convolved, and its
    output is marked with a zero beam, which convolve-rs writes, so that the
    cube it is combined into records that the channel holds no PSF.

    Args:
        plane (Path): The channel image to convolve
        beam_shape (BeamShape): The resolution to convolve to
        cutoff (float | None, optional): Planes whose major axis exceeds this, in arcsec, are blanked. Defaults to no cutoff.
        convol_suffix (str, optional): The suffix added to .fits to indicate a smoothed image. Defaults to 'conv'.

    Returns:
        Path: The convolved (or blanked) image
    """
    return convolve_images(
        image_paths=[plane],
        beam_shape=beam_shape,
        cutoff=cutoff,
        convol_suffix=convol_suffix,
    )[0]


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

    cubes = convolve_rs.smooth_cubes(
        list(cube_paths), mode="natural", cutoff=cutoff, dryrun=True
    )
    return [BeamShape.from_convolve_rs(beam) for beam in cubes[0].targets]


def convolve_cubes(
    cube_paths: Collection[Path],
    beam_shapes: list[BeamShape],
    cutoff: float | None = None,
    convol_suffix: str = "conv",
    executor_type: Literal["thread", "process", "mpi"] = "thread",
) -> Collection[Path]:
    """Convolve each channel of each cube to its beam in ``beam_shapes``.

    A channel whose beam is not finite, or whose own beam is coarser than
    ``cutoff``, is blanked and masked in the output's beam table.

    Args:
        cube_paths (Collection[Path]): The cubes to convolve
        beam_shapes (list[BeamShape]): The beam of each channel
        cutoff (float | None, optional): Channels whose major axis exceeds this, in arcsec, are blanked. Defaults to no cutoff.
        convol_suffix (str, optional): The suffix added to .fits to indicate a smoothed cube. Defaults to 'conv'.
        executor_type (Literal["thread", "process", "mpi"], optional): Unused, kept for callers: convolve-rs runs on threads. Defaults to "thread".

    Returns:
        Collection[Path]: The convolved cubes
    """
    logger.info(f"Will attempt to convol {len(cube_paths)} cubes")
    if cutoff:
        logger.info(f"Supplied cutoff {cutoff}")

    cubes = convolve_rs.smooth_cubes(
        list(cube_paths),
        mode="natural",
        target_beams=[beam.to_convolve_rs() for beam in beam_shapes],
        cutoff=cutoff,
        suffix=convol_suffix,
    )
    for cube in cubes:
        logger.info(f"{cube.input} convolved to {cube.output}")
    return [cube.output for cube in cubes]


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
        result = convolve_rs.smooth_images(
            list(image_paths), cutoff=cutoff, dryrun=True
        )
        beam_shape = BeamShape.from_convolve_rs(result.target)
        logger.info(f"Constructed {beam_shape=}")
    except ValueError:
        logger.info("The beam was not constrained. Setting to NaNs")
        beam_shape = BeamShape(bmaj_arcsec=np.nan, bmin_arcsec=np.nan, bpa_deg=np.nan)

    return beam_shape


def lost_in_convolution(image_path: Path, convolved_path: Path) -> bool:
    """Whether an image with data came out of convolve-rs all NaN yet still claiming a beam"""
    with fits.open(convolved_path, memmap=True) as convolved:
        if np.isfinite(convolved[0].data).any():
            return False
        if not header_beam_is_usable(header=convolved[0].header):
            return False
    with fits.open(image_path, memmap=True) as image:
        return bool(np.isfinite(image[0].data).any())


def convolve_images(
    image_paths: Collection[Path],
    beam_shape: BeamShape,
    cutoff: float | None = None,
    convol_suffix: str = "conv",
    output_paths: list[Path] | None = None,
) -> list[Path]:
    """Convolve a set of input images to a common resolution as specified
    by the beam_shape. If the major-axis of the native resolution is larger
    than cutoff (in arcseconds) then convolve-rs blanks it and gives it a
    zero beam.

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

    target_beam = beam_shape.to_convolve_rs()

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
        if not header_beam_is_usable(header=header):
            logger.info(f"Copying {image_path} to {convol_output_path=} for empty beam")
            copyfile(image_path, convol_output_path)
        else:
            logger.info(f"Convolving {image_path.name!s}")
            result = convolve_rs.smooth_images(
                [image_path],
                target_beam=target_beam,
                cutoff=cutoff,
                suffix=convol_suffix,
            )
            convol_output_path = result.images[0].output
            if lost_in_convolution(image_path, convol_output_path):
                logger.error(
                    f"convolve-rs turned {image_path.name} into all NaNs while "
                    f"claiming {beam_shape=}. Blanking it with a zero beam."
                )
                blank_images([convol_output_path], convol_suffix=None)

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


def blank_images(
    image_paths: Collection[Path], convol_suffix: str | None = "conv"
) -> list[Path]:
    """Blank each image to all-NaN, marked with a zero beam. The blank is a copy
    named with ``convol_suffix``, or the image itself when that is None.

    The copy stands in for a convolution that cannot be done: a channel whose
    every image is beyond the beam cutoff has no common beam to convolve to, and
    copying the images at their own resolutions would have whatever they are
    co-added into claim a beam that describes none of them. The zero beam is the
    marker ``usable_beam_mask`` and ``fitscube`` both read as 'no PSF here'.

    Args:
        image_paths (Collection[Path]): The images to blank
        convol_suffix (str | None, optional): The suffix added to .fits, matching the convolved images these stand in for. None blanks each image in place. Defaults to 'conv'.

    Returns:
        list[Path]: The blanked images
    """
    blank_image_paths: list[Path] = []

    for image_path in image_paths:
        blank_image_path = Path(image_path)
        if convol_suffix is not None:
            blank_image_path = Path(
                str(image_path).replace(".fits", f".{convol_suffix}.fits")
            )
        logger.info(f"Blanking {image_path} into {blank_image_path=}")

        with fits.open(image_path) as open_fits:
            header = open_fits[0].header.copy()
            data = np.full_like(open_fits[0].data, np.nan, dtype=np.float32)

        for key in ("BMAJ", "BMIN", "BPA"):
            header[key] = 0.0

        fits.writeto(blank_image_path, data=data, header=header, overwrite=True)
        blank_image_paths.append(blank_image_path)

    return blank_image_paths


def convolve_images_or_blank(
    image_paths: Collection[Path],
    beam_shape: BeamShape,
    cutoff: float | None = None,
    convol_suffix: str = "conv",
) -> list[Path]:
    """Convolve images to ``beam_shape``, or blank them when it does not describe
    a beam.

    ``convolve_images`` copies its inputs through untouched when handed a
    non-finite ``beam_shape``, which is what ``get_common_beam`` returns when
    every input is beyond the cutoff. For a set that is about to be co-added and
    cubed that copy is worse than nothing: the images keep their own differing
    resolutions and the product claims the first of them. Blank instead.

    Args:
        image_paths (Collection[Path]): The images to convolve
        beam_shape (BeamShape): The resolution to convolve to
        cutoff (float | None, optional): Images whose major axis exceeds this, in arcsec, are blanked. Defaults to no cutoff.
        convol_suffix (str, optional): The suffix added to .fits to indicate a smoothed image. Defaults to 'conv'.

    Returns:
        list[Path]: The convolved (or blanked) images
    """
    if np.isfinite(beam_shape.bmaj_arcsec):
        return convolve_images(
            image_paths=image_paths,
            beam_shape=beam_shape,
            cutoff=cutoff,
            convol_suffix=convol_suffix,
        )

    logger.info(
        f"No common beam over {len(image_paths)} images, likely all beyond "
        f"{cutoff=}. Blanking them rather than copying them through."
    )
    return blank_images(image_paths=image_paths, convol_suffix=convol_suffix)


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
