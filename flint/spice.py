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
from astropy.wcs.utils import skycoord_to_pixel
from radio_beam import Beam

from flint.coadd.linmos import BoundingBox, _merge_bound_boxes
from flint.convol import BeamShape
from flint.logging import logger
from flint.options import BaseOptions, SpiceOptions


class SkyBoundingBox(BaseOptions):
    """A bounding box on the sky, held as angular offsets from a reference position.

    Offsets are on-sky angles (the longitude offset already carries the cos(dec)
    factor), so a box may be projected onto any image's pixel grid.
    """

    reference_ra_deg: float
    """Right ascension of the reference position, in degrees"""
    reference_dec_deg: float
    """Declination of the reference position, in degrees"""
    lon_min_arcsec: float
    """Smallest longitude offset from the reference position"""
    lon_max_arcsec: float
    """Largest longitude offset from the reference position"""
    lat_min_arcsec: float
    """Smallest latitude offset from the reference position"""
    lat_max_arcsec: float
    """Largest latitude offset from the reference position"""

    @property
    def corners(self) -> SkyCoord:
        """The four corners of the box"""
        reference = SkyCoord(
            self.reference_ra_deg * u.deg, self.reference_dec_deg * u.deg
        )
        lon = (
            np.array(
                [
                    self.lon_min_arcsec,
                    self.lon_max_arcsec,
                    self.lon_max_arcsec,
                    self.lon_min_arcsec,
                ]
            )
            * u.arcsec
        )
        lat = (
            np.array(
                [
                    self.lat_min_arcsec,
                    self.lat_min_arcsec,
                    self.lat_max_arcsec,
                    self.lat_max_arcsec,
                ]
            )
            * u.arcsec
        )
        return reference.spherical_offsets_by(lon, lat)

    def to_bounding_box(
        self, wcs: WCS, image_shape: tuple[int, int]
    ) -> BoundingBox | None:
        """Project onto an image's pixel grid, or None if the box falls off it

        Args:
            wcs (WCS): WCS of the target image
            image_shape (tuple[int, int]): Shape of a single plane of the target

        Returns:
            BoundingBox | None: The clipped pixel box, or None if fully off-image
        """
        x_pix, y_pix = skycoord_to_pixel(
            wcs=wcs.celestial, coords=self.corners, origin=0
        )

        # BoundingBox.x* is numpy axis -2 (FITS NAXIS2/Dec); .y* is axis -1 (NAXIS1/RA)
        return BoundingBox(
            xmin=int(math.floor(float(np.min(y_pix)))),
            xmax=int(math.ceil(float(np.max(y_pix)))) + 1,
            ymin=int(math.floor(float(np.min(x_pix)))),
            ymax=int(math.ceil(float(np.max(x_pix)))) + 1,
            original_shape=image_shape,
        ).clip()


def _merge_sky_boxes(sky_boxes: list[SkyBoundingBox]) -> SkyBoundingBox:
    """Smallest sky box containing all inputs. All must share a reference position"""
    references = {(b.reference_ra_deg, b.reference_dec_deg) for b in sky_boxes}
    assert len(references) == 1, f"Sky boxes span differing references: {references}"

    return sky_boxes[0].with_options(
        lon_min_arcsec=min(b.lon_min_arcsec for b in sky_boxes),
        lon_max_arcsec=max(b.lon_max_arcsec for b in sky_boxes),
        lat_min_arcsec=min(b.lat_min_arcsec for b in sky_boxes),
        lat_max_arcsec=max(b.lat_max_arcsec for b in sky_boxes),
    )


def load_component_table(catalogue: Path | Table) -> Table:
    """Return a table given either a loaded table or a path to one on disk"""
    return Table.read(catalogue) if isinstance(catalogue, Path) else catalogue


def _resolve_columns(
    table: Table, spice_options: SpiceOptions, is_user_catalogue: bool
) -> tuple[str | None, str, str, tuple[str, str, str] | None]:
    """Resolve (island_col, ra_col, dec_col, shape_cols) for a catalogue."""
    if not is_user_catalogue:
        island_col = "island" if "island" in table.colnames else None
        return island_col, "ra", "dec", ("a", "b", "pa")

    if (
        spice_options.catalogue_ra_col is None
        or spice_options.catalogue_dec_col is None
    ):
        raise ValueError(
            "catalogue_ra_col and catalogue_dec_col must be set on SpiceOptions"
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


def _component_sky_box(
    row: Row,
    ra_col: str,
    dec_col: str,
    shape_cols: tuple[str, str, str] | None,
    radec_unit: u.Unit,
    shape_unit: u.Unit,
    reference: SkyCoord,
    deconvolved: bool,
    psf_beam: Beam | None,
) -> SkyBoundingBox:
    """Sky box of a single catalogue row's ellipse, or point if shape_cols is None.

    No beamwidth padding applied here.
    """
    sky_coord = SkyCoord(
        float(row[ra_col]) * radec_unit, float(row[dec_col]) * radec_unit
    )
    d_lon, d_lat = reference.spherical_offsets_to(sky_coord)
    lon_arcsec = d_lon.to(u.arcsec).value
    lat_arcsec = d_lat.to(u.arcsec).value

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

    return SkyBoundingBox(
        reference_ra_deg=float(reference.ra.deg),
        reference_dec_deg=float(reference.dec.deg),
        lon_min_arcsec=lon_arcsec - half_ra_arcsec,
        lon_max_arcsec=lon_arcsec + half_ra_arcsec,
        lat_min_arcsec=lat_arcsec - half_dec_arcsec,
        lat_max_arcsec=lat_arcsec + half_dec_arcsec,
    )


def island_sky_boxes(
    table: Table,
    wcs: WCS,
    beam_shape: BeamShape,
    spice_options: SpiceOptions,
    is_user_catalogue: bool,
) -> list[SkyBoundingBox]:
    """Per-island sky boxes: group catalogue rows by island, merge each island's
    component ellipses into one box, then pad by ``spice_options.n_beamwidths``
    restoring-beamwidths.

    Boxes are returned on the sky, referenced to ``wcs``'s reference position.
    Projection onto a pixel grid happens per target image in ``spice_fits``.
    """
    island_col, ra_col, dec_col, shape_cols = _resolve_columns(
        table=table, spice_options=spice_options, is_user_catalogue=is_user_catalogue
    )
    if len(table) == 0:
        raise ValueError("Catalogue is empty!")

    # Aegean's own catalogue is always degrees/arcsec see _resolve_columns
    radec_unit = u.Unit(
        "deg" if not is_user_catalogue else spice_options.catalogue_radec_unit
    )
    shape_unit = u.Unit(
        "arcsec" if not is_user_catalogue else spice_options.catalogue_shape_unit
    )
    deconvolved = bool(is_user_catalogue and spice_options.catalogue_sizes_deconvolved)
    reference = SkyCoord(*wcs.celestial.wcs.crval * u.deg)

    groups = (
        list(table.group_by(island_col).groups)
        if island_col is not None
        else [table[i : i + 1] for i in range(len(table))]
    )

    island_sky_boxes: list[SkyBoundingBox] = []
    for group in groups:
        component_boxes = [
            _component_sky_box(
                row=row,
                ra_col=ra_col,
                dec_col=dec_col,
                shape_cols=shape_cols,
                radec_unit=radec_unit,
                shape_unit=shape_unit,
                reference=reference,
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
        island_sky_boxes.append(_merge_sky_boxes(sky_boxes=component_boxes))

    pad_arcsec = spice_options.n_beamwidths * max(
        beam_shape.bmaj_arcsec, beam_shape.bmin_arcsec
    )
    padded_boxes = [
        box.with_options(
            lon_min_arcsec=box.lon_min_arcsec - pad_arcsec,
            lon_max_arcsec=box.lon_max_arcsec + pad_arcsec,
            lat_min_arcsec=box.lat_min_arcsec - pad_arcsec,
            lat_max_arcsec=box.lat_max_arcsec + pad_arcsec,
        )
        for box in island_sky_boxes
    ]

    logger.info(f"Built {len(padded_boxes)} island sky boxes")
    return padded_boxes


def keep_mask_from_boxes(
    bounding_boxes: list[BoundingBox], image_shape: tuple[int, int]
) -> np.ndarray:
    """Boolean mask, True inside the union of the supplied boxes"""
    mask = np.zeros(image_shape, dtype=bool)
    for bounding_box in bounding_boxes:
        mask[
            bounding_box.xmin : bounding_box.xmax,
            bounding_box.ymin : bounding_box.ymax,
        ] = True
    return mask


def any_box_overlaps(fits_path: Path, sky_boxes: list[SkyBoundingBox]) -> bool:
    """Whether any sky box projects onto ``fits_path``. Reads only the header

    Args:
        fits_path (Path): The image or cube to test
        sky_boxes (list[SkyBoundingBox]): Sky boxes to project

    Returns:
        bool: True if at least one box overlaps
    """
    header = fits.getheader(fits_path)
    wcs = WCS(header)
    image_shape = (header["NAXIS2"], header["NAXIS1"])

    return any(
        sky_box.to_bounding_box(wcs=wcs, image_shape=image_shape) is not None
        for sky_box in sky_boxes
    )


def spice_fits(
    fits_path: Path, sky_boxes: list[SkyBoundingBox], output_path: Path | None = None
) -> Path:
    """Mask every pixel outside ``boxes`` to NaN and crop to their union.

    Boxes are projected through this file's own WCS, so it need not share a pixel
    grid with the image they were built from. Works on a single plane or a
    multi-channel cube. A file that no box overlaps is left untouched.

    Args:
        fits_path (Path): The image or cube to spice
        sky_boxes (list[SkyBoundingBox]): Sky boxes to keep
        output_path (Path | None, optional): Directory to write the spiced cube into, deleting ``fits_path`` once written. Defaults to replacing ``fits_path`` in place.

    Returns:
        Path: The spiced cube, or ``fits_path`` unchanged if no box overlapped it
    """
    if not sky_boxes:
        raise ValueError(f"No bounding boxes supplied for {fits_path}")

    # A dask worker death after the unlink below leaves the output written and the
    # input gone. Recognise that state rather than failing the rerun.
    if output_path is not None:
        spiced_path = output_path / fits_path.name
        if spiced_path.exists() and not fits_path.exists():
            logger.info(f"{spiced_path} already written, nothing to redo")
            return spiced_path

    with fits.open(fits_path) as hdul:
        header = hdul[0].header.copy()
        image_shape = tuple(hdul[0].data.shape[-2:])
        wcs = WCS(header)

        pixel_boxes = [
            pixel_box
            for pixel_box in (
                sky_box.to_bounding_box(wcs=wcs, image_shape=image_shape)
                for sky_box in sky_boxes
            )
            if pixel_box is not None
        ]
        if not pixel_boxes:
            logger.warning(
                f"No island overlaps {fits_path}, leaving it unspiced. "
                f"Checked {len(sky_boxes)} sky boxes against shape {image_shape}"
            )
            return fits_path

        overall = _merge_bound_boxes(bounding_boxes=pixel_boxes)
        mask = keep_mask_from_boxes(bounding_boxes=pixel_boxes, image_shape=image_shape)
        mask_cropped = mask[overall.xmin : overall.xmax, overall.ymin : overall.ymax]
        data = np.array(
            hdul[0].data[..., overall.xmin : overall.xmax, overall.ymin : overall.ymax]
        )
        extra_hdus = [hdu.copy() for hdu in hdul[1:]]

    data[..., ~mask_cropped] = np.nan

    header["CRPIX1"] -= overall.ymin
    header["CRPIX2"] -= overall.xmin
    header.add_history(
        f"flint.spice: masked outside {len(pixel_boxes)} island boxes and cropped "
        f"{image_shape} -> {data.shape[-2:]}"
    )

    spiced_path = fits_path
    if output_path is not None:
        output_path.mkdir(parents=True, exist_ok=True)
        spiced_path = output_path / fits_path.name

    logger.info(f"Spicing {fits_path}: {image_shape} -> {data.shape[-2:]}")
    fits.HDUList([fits.PrimaryHDU(data=data, header=header), *extra_hdus]).writeto(
        spiced_path, overwrite=True
    )
    if spiced_path != fits_path:
        logger.info(f"Removing the unspiced {fits_path}")
        fits_path.unlink()

    return spiced_path
