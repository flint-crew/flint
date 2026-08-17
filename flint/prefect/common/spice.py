"""Prefect task wrappers for SPICE-style cube trimming (``flint.spice``)."""

from __future__ import annotations

from pathlib import Path

from astropy.io import fits
from astropy.wcs import WCS
from fitscube.combine_fits import compress_cube
from prefect import task

from flint.coadd.linmos import BoundingBox
from flint.convol import BeamShape
from flint.options import SpiceOptions
from flint.spice import island_bounding_boxes, load_component_table, spice_fits


@task
def task_get_spice_boxes(
    reference_image: Path,
    catalogue: Path,
    spice_options: SpiceOptions,
    beam_shape: BeamShape,
    is_user_catalogue: bool,
) -> list[BoundingBox]:
    """Build the (single, shared) set of island bounding boxes for a field,
    against ``reference_image``'s WCS/shape -- reused as-is for every
    Stokes/plane/cube ``task_spice_fits`` is later applied to."""
    header = fits.getheader(reference_image)
    wcs = WCS(header)
    image_shape = (header["NAXIS2"], header["NAXIS1"])

    return island_bounding_boxes(
        table=load_component_table(catalogue),
        wcs=wcs,
        image_shape=image_shape,
        beam_shape=beam_shape,
        spice_options=spice_options,
        is_user_catalogue=is_user_catalogue,
    )


task_spice_fits = task(spice_fits)
task_compress_cube = task(compress_cube)
