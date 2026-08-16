"""SPICE-style cube trimming: mask everything outside small boxes around
catalogued sources, crop to their union. See ``flint.options.SpiceOptions``.
"""

from __future__ import annotations

import math
from pathlib import Path

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.table import Row, Table
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales, skycoord_to_pixel
from radio_beam import Beam

from flint.coadd.linmos import BoundingBox, _merge_bound_boxes
from flint.convol import BeamShape
from flint.exceptions import ShapeMismatchError
from flint.logging import logger
from flint.options import SpiceOptions


def load_component_table(catalogue: Path | Table) -> Table:
    """Return a table given either a loaded table or a path to one on disk"""
    return Table.read(catalogue) if isinstance(catalogue, Path) else catalogue


def _resolve_columns(
    table: Table, spice_options: SpiceOptions, is_user_catalogue: bool
) -> tuple[str | None, str, str, tuple[str, str, str] | None]:
    """Resolve (island_col, ra_col, dec_col, shape_cols) for a catalogue.

    Column names/units are never guessed for a user-supplied catalogue --
    only the built-in Aegean catalogue has a fixed, known contract (confirmed
    against the installed aegeantools==2.3.0 source, AegeanTools.source_finder
    around result_to_components: island/ra/dec (deg) /a/b/pa (arcsec/arcsec/deg),
    with a/b/pa the as-fit, PSF-convolved sizes -- no reconvolution needed here).
    """
    if not is_user_catalogue:
        island_col = "island" if "island" in table.colnames else None
        return island_col, "ra", "dec", ("a", "b", "pa")

    if (
        spice_options.catalogue_ra_col is None
        or spice_options.catalogue_dec_col is None
    ):
        raise ValueError(
            "catalogue_ra_col and catalogue_dec_col must be set on SpiceOptions "
            "for a user-supplied spice_catalogue -- these are never guessed"
        )

    shape_cols = (
        spice_options.catalogue_maj_col,
        spice_options.catalogue_min_col,
        spice_options.catalogue_pa_col,
    )
    if any(shape_cols) and not all(shape_cols):
        raise ValueError(
            "catalogue_maj_col/catalogue_min_col/catalogue_pa_col must be all set "
            "or all unset"
        )
    if all(shape_cols) and spice_options.catalogue_sizes_deconvolved is None:
        raise ValueError(
            "catalogue_sizes_deconvolved must be set (True or False) whenever "
            "catalogue_maj_col is set -- never guessed"
        )

    return (
        spice_options.catalogue_island_col,
        spice_options.catalogue_ra_col,
        spice_options.catalogue_dec_col,
        shape_cols if all(shape_cols) else None,  # type: ignore[return-value]
    )


def _row_psf_beam(
    row: Row,
    spice_options: SpiceOptions,
    is_user_catalogue: bool,
    common_beam_shape: BeamShape,
) -> Beam:
    """The PSF to re-convolve a deconvolved source size with: per-row columns
    if given, otherwise the pipeline's common restoring beam."""
    psf_cols = (
        spice_options.catalogue_psf_maj_col,
        spice_options.catalogue_psf_min_col,
        spice_options.catalogue_psf_pa_col,
    )
    if is_user_catalogue and all(psf_cols):
        shape_unit = u.Unit(spice_options.catalogue_shape_unit)
        return Beam(
            major=float(row[psf_cols[0]]) * shape_unit,
            minor=float(row[psf_cols[1]]) * shape_unit,
            pa=float(row[psf_cols[2]]) * u.deg,
        )
    return Beam(
        major=common_beam_shape.bmaj_arcsec * u.arcsec,
        minor=common_beam_shape.bmin_arcsec * u.arcsec,
        pa=common_beam_shape.bpa_deg * u.deg,
    )


def _component_box(
    row: Row,
    ra_col: str,
    dec_col: str,
    shape_cols: tuple[str, str, str] | None,
    radec_unit: u.Unit,
    shape_unit: u.Unit,
    wcs: WCS,
    image_shape: tuple[int, int],
    pixel_scale_ra_arcsec: float,
    pixel_scale_dec_arcsec: float,
    deconvolved: bool,
    psf_beam: Beam | None,
) -> BoundingBox:
    """Bounding box of a single catalogue row's ellipse (or point, if
    shape_cols is None), in pixel space. No beamwidth padding applied here --
    that happens once per island, after merging."""
    sky_coord = SkyCoord(
        float(row[ra_col]) * radec_unit, float(row[dec_col]) * radec_unit
    )
    x_pix, y_pix = skycoord_to_pixel(wcs=wcs.celestial, coords=sky_coord, origin=0)

    half_ra_arcsec = half_dec_arcsec = 0.0
    if shape_cols is not None:
        maj = float(row[shape_cols[0]]) * shape_unit
        minor = float(row[shape_cols[1]]) * shape_unit
        pa = float(row[shape_cols[2]]) * u.deg
        if deconvolved:
            assert psf_beam is not None
            observed = Beam(major=maj, minor=minor, pa=pa).convolve(psf_beam)
            maj, minor, pa = observed.major, observed.minor, observed.pa

        # Semi-axes of the as-observed (FWHM) ellipse; pa is East of North.
        a = maj.to(u.arcsec).value / 2.0
        b = minor.to(u.arcsec).value / 2.0
        theta = pa.to(u.rad).value
        half_dec_arcsec = math.hypot(a * math.cos(theta), b * math.sin(theta))
        half_ra_arcsec = math.hypot(a * math.sin(theta), b * math.cos(theta))

    half_dec_pix = half_dec_arcsec / pixel_scale_dec_arcsec
    half_ra_pix = half_ra_arcsec / pixel_scale_ra_arcsec

    # BoundingBox.x* is numpy axis -2 (FITS NAXIS2/Dec); .y* is axis -1 (NAXIS1/RA)
    # -- the opposite of skycoord_to_pixel's (x=RA, y=Dec) astropy convention.
    return BoundingBox(
        xmin=int(math.floor(y_pix - half_dec_pix)),
        xmax=int(math.ceil(y_pix + half_dec_pix)) + 1,
        ymin=int(math.floor(x_pix - half_ra_pix)),
        ymax=int(math.ceil(x_pix + half_ra_pix)) + 1,
        original_shape=image_shape,
    )


def _clip_box(box: BoundingBox) -> BoundingBox | None:
    """Clip a box to its image, or None if it falls entirely off-image"""
    ny, nx = box.original_shape
    xmin, xmax = max(0, box.xmin), min(ny, box.xmax)
    ymin, ymax = max(0, box.ymin), min(nx, box.ymax)
    if xmin >= xmax or ymin >= ymax:
        return None
    return box.with_options(xmin=xmin, xmax=xmax, ymin=ymin, ymax=ymax)


def island_bounding_boxes(
    table: Table,
    wcs: WCS,
    image_shape: tuple[int, int],
    beam_shape: BeamShape,
    spice_options: SpiceOptions,
    is_user_catalogue: bool,
) -> list[BoundingBox]:
    """Per-island bounding boxes: group catalogue rows by island, merge each
    island's component ellipses into one box, then pad by
    ``spice_options.n_beamwidths`` restoring-beamwidths. Boxes that fall
    entirely off the image are dropped.
    """
    island_col, ra_col, dec_col, shape_cols = _resolve_columns(
        table=table, spice_options=spice_options, is_user_catalogue=is_user_catalogue
    )
    if len(table) == 0:
        raise ValueError("Catalogue is empty -- nothing to box")

    # Aegean's own catalogue is always degrees/arcsec -- see _resolve_columns
    radec_unit = u.Unit(
        "deg" if not is_user_catalogue else spice_options.catalogue_radec_unit
    )
    shape_unit = u.Unit(
        "arcsec" if not is_user_catalogue else spice_options.catalogue_shape_unit
    )
    deconvolved = bool(is_user_catalogue and spice_options.catalogue_sizes_deconvolved)
    pixel_scale_ra_arcsec, pixel_scale_dec_arcsec = proj_plane_pixel_scales(
        wcs.celestial
    ) * u.deg.to(u.arcsec)

    groups = (
        list(table.group_by(island_col).groups)
        if island_col is not None
        else [table[i : i + 1] for i in range(len(table))]
    )

    island_boxes: list[BoundingBox] = []
    for group in groups:
        component_boxes = [
            _component_box(
                row=row,
                ra_col=ra_col,
                dec_col=dec_col,
                shape_cols=shape_cols,
                radec_unit=radec_unit,
                shape_unit=shape_unit,
                wcs=wcs,
                image_shape=image_shape,
                pixel_scale_ra_arcsec=pixel_scale_ra_arcsec,
                pixel_scale_dec_arcsec=pixel_scale_dec_arcsec,
                deconvolved=deconvolved,
                psf_beam=_row_psf_beam(
                    row=row,
                    spice_options=spice_options,
                    is_user_catalogue=is_user_catalogue,
                    common_beam_shape=beam_shape,
                )
                if deconvolved
                else None,
            )
            for row in group
        ]
        island_boxes.append(_merge_bound_boxes(bounding_boxes=component_boxes))

    pad_arcsec = spice_options.n_beamwidths * max(
        beam_shape.bmaj_arcsec, beam_shape.bmin_arcsec
    )
    pad_dec_pix = math.ceil(pad_arcsec / pixel_scale_dec_arcsec)
    pad_ra_pix = math.ceil(pad_arcsec / pixel_scale_ra_arcsec)

    padded_boxes = [
        box.with_options(
            xmin=box.xmin - pad_dec_pix,
            xmax=box.xmax + pad_dec_pix,
            ymin=box.ymin - pad_ra_pix,
            ymax=box.ymax + pad_ra_pix,
        )
        for box in island_boxes
    ]
    clipped_boxes = [box for box in map(_clip_box, padded_boxes) if box is not None]

    if not clipped_boxes:
        raise ValueError("No island bounding boxes remain after clipping to the image")

    logger.info(f"Built {len(clipped_boxes)} island bounding boxes")
    return clipped_boxes


def keep_mask_from_boxes(
    boxes: list[BoundingBox], image_shape: tuple[int, int]
) -> np.ndarray:
    """Boolean mask, True inside the union of the supplied boxes"""
    mask = np.zeros(image_shape, dtype=bool)
    for box in boxes:
        mask[box.xmin : box.xmax, box.ymin : box.ymax] = True
    return mask


def spice_fits(fits_path: Path, boxes: list[BoundingBox]) -> Path:
    """Mask every pixel outside ``boxes`` to NaN and crop to their union,
    replacing ``fits_path`` in place. Works on a single plane or a
    multi-channel cube -- the mask/crop broadcast across any leading axes.
    """
    if not boxes:
        raise ValueError(f"No bounding boxes supplied for {fits_path}")

    overall = _merge_bound_boxes(bounding_boxes=boxes)
    mask = keep_mask_from_boxes(boxes=boxes, image_shape=overall.original_shape)
    mask_cropped = mask[overall.xmin : overall.xmax, overall.ymin : overall.ymax]

    with fits.open(fits_path) as hdul:
        image_shape = tuple(hdul[0].data.shape[-2:])
        if image_shape != tuple(overall.original_shape):
            raise ShapeMismatchError(
                f"{fits_path} has shape {image_shape}, but boxes were built "
                f"against {overall.original_shape}"
            )
        header = hdul[0].header.copy()
        data = np.array(
            hdul[0].data[..., overall.xmin : overall.xmax, overall.ymin : overall.ymax]
        )
        extra_hdus = [hdu.copy() for hdu in hdul[1:]]

    data[..., ~mask_cropped] = np.nan

    header["CRPIX1"] -= overall.ymin
    header["CRPIX2"] -= overall.xmin

    logger.info(f"Spicing {fits_path}: {overall.original_shape} -> {data.shape[-2:]}")
    fits.HDUList([fits.PrimaryHDU(data=data, header=header), *extra_hdus]).writeto(
        fits_path, overwrite=True
    )

    return fits_path
