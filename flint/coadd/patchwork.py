"""This is an interface into patchwork, which rotates Stokes Q and U into the
frame of the mosaic as it co-adds them."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Literal

from astropy.coordinates import SkyCoord
from astropy.io import fits
from capn_crunch import BaseOptions
from patchwork.io import BeamShape
from patchwork.mosaic import mosaic
from patchwork.options import (
    FrameOptions,
    MosaicOptions,
    PolarisationOptions,
    PrimaryBeamOptions,
    ResampleOptions,
)
from patchwork.polarisation import STOKES_CODES
from patchwork.tile import weight_groups

from flint.coadd.linmos import compute_pb_rotation_alpha
from flint.logging import logger
from flint.naming import create_linmos_names


class PatchworkOptions(BaseOptions):
    """Options that direct how patchwork mosaics"""

    kernel: Literal[
        "nearest", "linear", "cubic", "lanczos2", "lanczos3", "lanczos4"
    ] = "cubic"
    """Interpolation the inputs are resampled with"""
    backend: Literal["auto", "numpy", "numba", "fused"] = "fused"
    """How a tile is computed. Only ``fused`` spreads one over ``tile_threads``"""
    tile_size: int = 1024
    """Size in pixels of the square tiles the mosaic is made in"""
    tile_threads: int = 0
    """Threads each tile is made with. 0 uses every CPU the worker may run on"""
    holography_scale: Literal["centre", "stored"] = "centre"
    """Scale each holography beam to 1 at its centre, or use it as stored, which is what linmos does"""
    output_frame: Literal["north", "centre"] = "north"
    """Reference Q and U to north at each pixel, the IAU convention, or to north at the centre of the mosaic"""


class MosaicResult(BaseOptions):
    """The mosaic of one Stokes"""

    image_fits: Path
    """The co-added image"""
    weight_fits: Path
    """The weight of the co-added image"""


def _first_usable_beam(images: Collection[Path]) -> BeamShape | None:
    """The restoring beam of the first image that has one"""
    for image in images:
        beam = BeamShape.from_header(fits.getheader(image))
        if beam is not None and beam.is_usable():
            return beam
    return None


def patchwork_stokes_images(
    stokes_images: Mapping[str, Collection[Path]],
    stokes_base_paths: Mapping[str, Path],
    patchwork_options: PatchworkOptions,
    pb_cutoff: float = 0.1,
    holofile: Path | None = None,
    pol_axis: float | None = None,
    boresight: SkyCoord | None = None,
    trim: bool = False,
) -> dict[str, MosaicResult]:
    """Mosaic the beam images of every Stokes of one channel in a single pass.

    Q and U are only rotated when they are mosaicked together, so every Stokes
    goes in at once. The outputs take the names linmos would give them.

    Args:
        stokes_images (Mapping[str, Collection[Path]]): The beam images of each Stokes, keyed 'i', 'q', 'u' or 'v'
        stokes_base_paths (Mapping[str, Path]): The base path of each Stokes' outputs, as ``create_linmos_names`` takes it
        patchwork_options (PatchworkOptions): How patchwork resamples and scales
        pb_cutoff (float, optional): A beam contributes only where its primary beam is at least this. Defaults to 0.1.
        holofile (Path | None, optional): Holography cube to weight by and remove leakage with. Defaults to None, a Gaussian beam.
        pol_axis (float | None, optional): Third-axis orientation of the field in radians, which rotates the holography. Defaults to None.
        boresight (SkyCoord | None, optional): Where the dish pointed, which Q and U are referenced to north at. Defaults to None.
        trim (bool, optional): Trim the mosaic to where the beams contribute. That follows the primary beam, and so the frequency, so it is not for channels that are stacked into a cube. Defaults to False.

    Returns:
        dict[str, MosaicResult]: The mosaic and weight of each Stokes
    """
    images = [image for beam_images in stokes_images.values() for image in beam_images]
    logger.info(
        f"Mosaicking {len(images)} images of Stokes {list(stokes_images)} with patchwork"
    )

    options = MosaicOptions(
        frame=FrameOptions(full_frame=not trim),
        primary_beam=PrimaryBeamOptions(
            pb_model="holography" if holofile else "gaussian",
            holography_cube=holofile,
            holography_alpha_rad=(
                compute_pb_rotation_alpha(pol_axis=pol_axis)
                if pol_axis is not None
                else 0.0
            ),
            holography_scale=patchwork_options.holography_scale,
            pb_cutoff=pb_cutoff,
        ),
        resample=ResampleOptions(
            kernel=patchwork_options.kernel,
            tile_size=patchwork_options.tile_size,
            backend=patchwork_options.backend,
            tile_threads=patchwork_options.tile_threads,
        ),
        polarisation=PolarisationOptions(
            frame_centre=(
                (boresight.ra.deg, boresight.dec.deg) if boresight is not None else None
            ),
            output_frame=patchwork_options.output_frame,
            remove_leakage=(
                holofile is not None and "i" in stokes_images and len(stokes_images) > 1
            ),
        ),
    )

    # patchwork would take the beam of the first image, which may be a blanked one
    beam = _first_usable_beam(images)
    scratch = Path(f"{next(iter(stokes_base_paths.values()))}.patchwork")
    scratch_names = mosaic(
        images=images, output_base=scratch, options=options, beam=beam, overwrite=True
    )

    if beam is None:
        # No input had a beam, so the mosaics carry flint's marker for having none
        for image in scratch_names.images:
            with fits.open(image, mode="update") as open_fits:
                for key in ("BMAJ", "BMIN", "BPA"):
                    open_fits[0].header[key] = 0.0

    # patchwork orders its mosaics by Stokes code, and says which weight each has.
    # Q and U share one, so every name is a hard link to the output
    ordered_stokes = sorted(stokes_base_paths, key=lambda s: STOKES_CODES[s.upper()])
    groups = weight_groups(tuple(STOKES_CODES[s.upper()] for s in ordered_stokes))
    results: dict[str, MosaicResult] = {}
    for stokes, scratch_image, group in zip(
        ordered_stokes, scratch_names.images, groups, strict=True
    ):
        linmos_names = create_linmos_names(name_prefix=stokes_base_paths[stokes])
        for scratch_path, final_path in (
            (scratch_image, linmos_names.image_fits),
            (scratch_names.weights[group], linmos_names.weight_fits),
        ):
            final_path.unlink(missing_ok=True)
            final_path.hardlink_to(scratch_path)
        results[stokes] = MosaicResult(
            image_fits=linmos_names.image_fits, weight_fits=linmos_names.weight_fits
        )

    for scratch_path in (*scratch_names.images, *scratch_names.weights):
        scratch_path.unlink()

    return results
