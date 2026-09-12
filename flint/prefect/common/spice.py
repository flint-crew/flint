"""Prefect task wrappers for SPICE-style cube trimming (``flint.spice``)."""

from __future__ import annotations

from pathlib import Path

from astropy.io import fits
from astropy.wcs import WCS
from fitscube.combine_fits import compress_cube

from flint.convol import BeamShape
from flint.options import SpiceOptions
from flint.prefect.caching import task
from flint.spice import (
    SkyBoundingBox,
    island_sky_boxes,
    load_component_table,
    spice_fits,
)


@task
def task_get_spice_boxes(
    reference_image: Path,
    catalogue: Path,
    spice_options: SpiceOptions,
    beam_shape: BeamShape,
    is_user_catalogue: bool,
) -> list[SkyBoundingBox]:
    """Build the set of island sky boxes for a field"""
    wcs = WCS(fits.getheader(reference_image))

    return island_sky_boxes(
        table=load_component_table(catalogue),
        wcs=wcs,
        beam_shape=beam_shape,
        spice_options=spice_options,
        is_user_catalogue=is_user_catalogue,
    )


task_spice_fits = task(spice_fits)
task_compress_cube = task(compress_cube)
