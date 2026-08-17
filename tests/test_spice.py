"""Tests for SPICE-style cube trimming (flint.spice): catalogue -> per-island
pixel boxes -> mask/crop-in-place. rm-lite/aegean's own science is not this
module's responsibility -- these tests check the WCS/box geometry and the
FITS in -> FITS out plumbing.
"""

from __future__ import annotations

import math
from pathlib import Path

import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.table import Table
from astropy.wcs import WCS
from astropy.wcs.utils import skycoord_to_pixel
from radio_beam import Beam

from flint.convol import BeamShape
from flint.options import SpiceOptions
from flint.spice import island_bounding_boxes, keep_mask_from_boxes, spice_fits

NY = 200
NX = 200
RA0, DEC0 = 180.0, -45.0
PIXEL_SCALE_ARCSEC = 1.0
BEAM = BeamShape(bmaj_arcsec=10.0, bmin_arcsec=8.0, bpa_deg=0.0)


def _make_wcs() -> WCS:
    wcs = WCS(naxis=2)
    wcs.wcs.crpix = [NX / 2, NY / 2]
    wcs.wcs.cdelt = [-PIXEL_SCALE_ARCSEC / 3600, PIXEL_SCALE_ARCSEC / 3600]
    wcs.wcs.crval = [RA0, DEC0]
    wcs.wcs.ctype = ["RA---SIN", "DEC--SIN"]
    return wcs


def _aegean_table(rows: list[dict]) -> Table:
    """Rows with island/ra/dec/a/b/pa/peak_flux/int_flux/local_rms -- matching
    the built-in Aegean component table's contract exactly."""
    return Table(rows=rows, names=list(rows[0].keys()))


def _default_row(
    island: int, ra: float, dec: float, a: float = 5.0, b: float = 5.0, pa: float = 0.0
) -> dict:
    return dict(
        island=island,
        ra=ra,
        dec=dec,
        a=a,
        b=b,
        pa=pa,
        peak_flux=1.0,
        int_flux=1.0,
        local_rms=0.01,
    )


def test_component_box_centred_on_source():
    wcs = _make_wcs()
    table = _aegean_table([_default_row(0, RA0, DEC0)])
    boxes = island_bounding_boxes(
        table=table,
        wcs=wcs,
        image_shape=(NY, NX),
        beam_shape=BEAM,
        spice_options=SpiceOptions(n_beamwidths=0.0),
        is_user_catalogue=False,
    )
    assert len(boxes) == 1
    box = boxes[0]
    x_pix, y_pix = skycoord_to_pixel(
        wcs=wcs, coords=SkyCoord(RA0 * u.deg, DEC0 * u.deg), origin=0
    )
    assert box.xmin <= y_pix <= box.xmax
    assert box.ymin <= x_pix <= box.ymax


def test_axis_convention_ra_moves_y_dec_moves_x():
    """The single highest-value test: RA offsets must only move ymin/ymax
    (NAXIS1), Dec offsets must only move xmin/xmax (NAXIS2). A centred,
    square test would not catch a transposition bug here."""
    wcs = _make_wcs()
    ra_offset_deg = 20.0 / 3600 / math.cos(math.radians(DEC0))
    table = _aegean_table(
        [_default_row(0, RA0, DEC0), _default_row(1, RA0 + ra_offset_deg, DEC0)]
    )
    boxes = island_bounding_boxes(
        table=table,
        wcs=wcs,
        image_shape=(NY, NX),
        beam_shape=BEAM,
        spice_options=SpiceOptions(n_beamwidths=0.0),
        is_user_catalogue=False,
    )
    b0, b1 = boxes
    assert b0.xmin == b1.xmin and b0.xmax == b1.xmax
    assert b0.ymin != b1.ymin and b0.ymax != b1.ymax

    table = _aegean_table(
        [_default_row(0, RA0, DEC0), _default_row(1, RA0, DEC0 + 20.0 / 3600)]
    )
    boxes = island_bounding_boxes(
        table=table,
        wcs=wcs,
        image_shape=(NY, NX),
        beam_shape=BEAM,
        spice_options=SpiceOptions(n_beamwidths=0.0),
        is_user_catalogue=False,
    )
    b0, b1 = boxes
    assert b0.ymin == b1.ymin and b0.ymax == b1.ymax
    assert b0.xmin != b1.xmin and b0.xmax != b1.xmax


def test_islands_merge_components_sharing_island_id():
    wcs = _make_wcs()
    offset_deg = 10.0 / 3600
    table = _aegean_table(
        [
            _default_row(0, RA0, DEC0),
            _default_row(0, RA0, DEC0 + offset_deg),
            _default_row(1, RA0 + 0.02, DEC0),
        ]
    )
    boxes = island_bounding_boxes(
        table=table,
        wcs=wcs,
        image_shape=(NY, NX),
        beam_shape=BEAM,
        spice_options=SpiceOptions(n_beamwidths=0.0),
        is_user_catalogue=False,
    )
    assert len(boxes) == 2
    merged = max(boxes, key=lambda b: b.xmax - b.xmin)
    assert merged.xmax - merged.xmin > offset_deg * 3600 / PIXEL_SCALE_ARCSEC


def test_padding_scales_linearly_with_n_beamwidths():
    wcs = _make_wcs()
    table = _aegean_table([_default_row(0, RA0, DEC0, a=0.0, b=0.0)])

    def _box(n_beamwidths):
        return island_bounding_boxes(
            table=table,
            wcs=wcs,
            image_shape=(NY, NX),
            beam_shape=BEAM,
            spice_options=SpiceOptions(n_beamwidths=n_beamwidths),
            is_user_catalogue=False,
        )[0]

    box0 = _box(0.0)
    box2 = _box(2.0)
    box4 = _box(4.0)
    span0 = box0.xmax - box0.xmin
    span2 = box2.xmax - box2.xmin
    span4 = box4.xmax - box4.xmin
    assert span2 > span0
    # doubling the padding roughly doubles the extra span added beyond span0
    assert math.isclose((span4 - span0), 2 * (span2 - span0), rel_tol=0.1)


def test_rotated_ellipse_extent_swaps_axes():
    wcs = _make_wcs()
    table_pa0 = _aegean_table([_default_row(0, RA0, DEC0, a=20.0, b=5.0, pa=0.0)])
    table_pa90 = _aegean_table([_default_row(0, RA0, DEC0, a=20.0, b=5.0, pa=90.0)])
    opts = SpiceOptions(n_beamwidths=0.0)

    box_pa0 = island_bounding_boxes(
        table=table_pa0,
        wcs=wcs,
        image_shape=(NY, NX),
        beam_shape=BEAM,
        spice_options=opts,
        is_user_catalogue=False,
    )[0]
    box_pa90 = island_bounding_boxes(
        table=table_pa90,
        wcs=wcs,
        image_shape=(NY, NX),
        beam_shape=BEAM,
        spice_options=opts,
        is_user_catalogue=False,
    )[0]
    # pa=0 -> major axis along Dec (x), pa=90 -> major axis along RA (y)
    assert (box_pa0.xmax - box_pa0.xmin) > (box_pa0.ymax - box_pa0.ymin)
    assert (box_pa90.ymax - box_pa90.ymin) > (box_pa90.xmax - box_pa90.xmin)


def test_boxes_clipped_and_dropped_at_image_edge():
    wcs = _make_wcs()
    table = _aegean_table([_default_row(0, RA0, DEC0)])
    boxes = island_bounding_boxes(
        table=table,
        wcs=wcs,
        image_shape=(NY, NX),
        beam_shape=BEAM,
        spice_options=SpiceOptions(n_beamwidths=1000.0),
        is_user_catalogue=False,
    )
    assert boxes[0].xmin == 0 and boxes[0].xmax == NY
    assert boxes[0].ymin == 0 and boxes[0].ymax == NX

    with pytest.raises(ValueError):
        island_bounding_boxes(
            table=_aegean_table([_default_row(0, RA0 + 10, DEC0)]),
            wcs=wcs,
            image_shape=(NY, NX),
            beam_shape=BEAM,
            spice_options=SpiceOptions(n_beamwidths=0.0),
            is_user_catalogue=False,
        )


def test_fallback_no_island_column_and_no_shape_columns():
    wcs = _make_wcs()
    table = Table({"ra": [RA0], "dec": [DEC0]})
    opts = SpiceOptions(
        catalogue_ra_col="ra", catalogue_dec_col="dec", n_beamwidths=2.0
    )
    boxes = island_bounding_boxes(
        table=table,
        wcs=wcs,
        image_shape=(NY, NX),
        beam_shape=BEAM,
        spice_options=opts,
        is_user_catalogue=True,
    )
    assert len(boxes) == 1
    pad_pix = math.ceil(2.0 * BEAM.bmaj_arcsec / PIXEL_SCALE_ARCSEC)
    box = boxes[0]
    assert (box.xmax - box.xmin) == 2 * pad_pix + 1
    assert (box.ymax - box.ymin) == 2 * pad_pix + 1


def test_keep_mask_union_area():
    boxes = island_bounding_boxes(
        table=_aegean_table(
            [_default_row(0, RA0, DEC0), _default_row(1, RA0 + 0.02, DEC0)]
        ),
        wcs=_make_wcs(),
        image_shape=(NY, NX),
        beam_shape=BEAM,
        spice_options=SpiceOptions(n_beamwidths=0.0),
        is_user_catalogue=False,
    )
    mask = keep_mask_from_boxes(boxes=boxes, image_shape=(NY, NX))
    expected = sum((b.xmax - b.xmin) * (b.ymax - b.ymin) for b in boxes)
    assert mask.sum() == expected


def test_deconvolved_sizes_reconvolved_larger_than_observed():
    wcs = _make_wcs()
    table = Table({"ra": [RA0], "dec": [DEC0], "maj": [5.0], "min": [5.0], "pa": [0.0]})
    common_opts = dict(
        catalogue_ra_col="ra",
        catalogue_dec_col="dec",
        catalogue_maj_col="maj",
        catalogue_min_col="min",
        catalogue_pa_col="pa",
        n_beamwidths=0.0,
    )
    box_observed = island_bounding_boxes(
        table=table,
        wcs=wcs,
        image_shape=(NY, NX),
        beam_shape=BEAM,
        spice_options=SpiceOptions(catalogue_sizes_deconvolved=False, **common_opts),
        is_user_catalogue=True,
    )[0]
    box_deconvolved = island_bounding_boxes(
        table=table,
        wcs=wcs,
        image_shape=(NY, NX),
        beam_shape=BEAM,
        spice_options=SpiceOptions(catalogue_sizes_deconvolved=True, **common_opts),
        is_user_catalogue=True,
    )[0]
    assert (box_deconvolved.xmax - box_deconvolved.xmin) > (
        box_observed.xmax - box_observed.xmin
    )

    expected = Beam(major=5 * u.arcsec, minor=5 * u.arcsec, pa=0 * u.deg).convolve(
        Beam(
            major=BEAM.bmaj_arcsec * u.arcsec,
            minor=BEAM.bmin_arcsec * u.arcsec,
            pa=BEAM.bpa_deg * u.deg,
        )
    )
    expected_span = math.ceil(expected.major.to(u.arcsec).value) + 2
    assert abs((box_deconvolved.xmax - box_deconvolved.xmin) - expected_span) <= 2


def test_deconvolved_requires_explicit_flag():
    wcs = _make_wcs()
    table = Table({"ra": [RA0], "dec": [DEC0], "maj": [5.0], "min": [5.0], "pa": [0.0]})
    opts = SpiceOptions(
        catalogue_ra_col="ra",
        catalogue_dec_col="dec",
        catalogue_maj_col="maj",
        catalogue_min_col="min",
        catalogue_pa_col="pa",
    )
    with pytest.raises(ValueError):
        island_bounding_boxes(
            table=table,
            wcs=wcs,
            image_shape=(NY, NX),
            beam_shape=BEAM,
            spice_options=opts,
            is_user_catalogue=True,
        )


def test_user_catalogue_requires_ra_dec_columns():
    wcs = _make_wcs()
    table = Table({"ra": [RA0], "dec": [DEC0]})
    with pytest.raises(ValueError):
        island_bounding_boxes(
            table=table,
            wcs=wcs,
            image_shape=(NY, NX),
            beam_shape=BEAM,
            spice_options=SpiceOptions(),
            is_user_catalogue=True,
        )


def _write_test_fits(
    path: Path, wcs: WCS, shape: tuple[int, ...], extra_hdu: bool = False
) -> None:
    header = wcs.to_header()
    header["BMAJ"] = BEAM.bmaj_arcsec / 3600
    header["BMIN"] = BEAM.bmin_arcsec / 3600
    header["BPA"] = BEAM.bpa_deg
    data = np.arange(math.prod(shape), dtype=np.float32).reshape(shape)
    hdus = [fits.PrimaryHDU(data=data, header=header)]
    if extra_hdu:
        hdus.append(
            fits.BinTableHDU.from_columns(
                [fits.Column(name="BMAJ", format="D", array=[1.0])], name="BEAMS"
            )
        )
    fits.HDUList(hdus).writeto(path, overwrite=True)


@pytest.mark.parametrize("shape", [(NY, NX), (3, 1, NY, NX)])
def test_spice_fits_masks_crops_and_replaces(tmp_path: Path, shape):
    wcs = _make_wcs()
    fits_path = tmp_path / "test.fits"
    _write_test_fits(fits_path, wcs, shape)

    table = _aegean_table(
        [_default_row(0, RA0, DEC0), _default_row(1, RA0 + 0.02, DEC0)]
    )
    boxes = island_bounding_boxes(
        table=table,
        wcs=wcs,
        image_shape=(NY, NX),
        beam_shape=BEAM,
        spice_options=SpiceOptions(n_beamwidths=1.0),
        is_user_catalogue=False,
    )

    out_path = spice_fits(fits_path=fits_path, boxes=boxes)
    assert out_path == fits_path

    with fits.open(out_path) as hdul:
        data = hdul[0].data
        header = hdul[0].header

    expected_span_x = max(b.xmax for b in boxes) - min(b.xmin for b in boxes)
    expected_span_y = max(b.ymax for b in boxes) - min(b.ymin for b in boxes)
    assert data.shape[-2:] == (expected_span_x, expected_span_y)
    assert data.shape[:-2] == tuple(shape[:-2])
    assert np.isfinite(data).any()
    assert not np.isfinite(data).all()

    # WCS round-trip: a catalogued source must still land inside the finite region
    out_wcs = WCS(header)
    x_pix, y_pix = skycoord_to_pixel(
        wcs=out_wcs, coords=SkyCoord(RA0 * u.deg, DEC0 * u.deg), origin=0
    )
    assert np.isfinite(data[..., int(np.round(y_pix)), int(np.round(x_pix))]).all()


def test_spice_fits_preserves_extra_hdus(tmp_path: Path):
    wcs = _make_wcs()
    fits_path = tmp_path / "test.fits"
    _write_test_fits(fits_path, wcs, (NY, NX), extra_hdu=True)

    table = _aegean_table([_default_row(0, RA0, DEC0)])
    boxes = island_bounding_boxes(
        table=table,
        wcs=wcs,
        image_shape=(NY, NX),
        beam_shape=BEAM,
        spice_options=SpiceOptions(n_beamwidths=1.0),
        is_user_catalogue=False,
    )
    spice_fits(fits_path=fits_path, boxes=boxes)

    with fits.open(fits_path) as hdul:
        assert len(hdul) == 2
        assert hdul[1].name == "BEAMS"


def test_spice_fits_no_boxes_raises(tmp_path: Path):
    wcs = _make_wcs()
    fits_path = tmp_path / "test.fits"
    _write_test_fits(fits_path, wcs, (NY, NX))
    with pytest.raises(ValueError):
        spice_fits(fits_path=fits_path, boxes=[])
