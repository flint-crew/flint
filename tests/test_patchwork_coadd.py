"""Tests around mosaicking with patchwork. These make real, small mosaics"""

from __future__ import annotations

from pathlib import Path

import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS

from flint.coadd.patchwork import (
    MosaicResult,
    PatchworkOptions,
    patchwork_images,
    patchwork_stokes_images,
)

BORESIGHT = SkyCoord(30.0 * u.deg, -80.0 * u.deg)
STOKES_CODES = {"i": 1, "q": 2, "u": 3, "v": 4}
# Compiling the default backend is left to the one test that is about it
NUMPY = PatchworkOptions(backend="numpy")


def write_beam_image(
    path: Path,
    centre: SkyCoord,
    stokes: str,
    value: float = 1.0,
    frequency_hz: float = 9.4e8,
    blank: bool = False,
) -> Path:
    """A noisy 64 pixel beam image of one value, or a blanked one"""
    size = 64
    header = fits.Header()
    axes = (
        ("RA---SIN", centre.ra.deg, -0.05, size / 2 + 1),
        ("DEC--SIN", centre.dec.deg, 0.05, size / 2 + 1),
        ("FREQ", frequency_hz, 1.0e6, 1),
        ("STOKES", STOKES_CODES[stokes], 1, 1),
    )
    for axis, (ctype, crval, cdelt, crpix) in enumerate(axes, start=1):
        header[f"CTYPE{axis}"] = ctype
        header[f"CRVAL{axis}"] = crval
        header[f"CDELT{axis}"] = cdelt
        header[f"CRPIX{axis}"] = crpix
    header["BMAJ"] = header["BMIN"] = 0.0 if blank else 0.01
    header["BPA"] = 0.0
    header["BUNIT"] = "Jy/beam"

    # A noiseless image has no measurable noise, so would be given no weight
    data = value + np.random.default_rng(0).normal(0, 0.01, (1, 1, size, size))
    if blank:
        data[:] = np.nan
    fits.writeto(path, data.astype(np.float32), header)
    return path


def beam_images(
    directory: Path,
    stokes: str,
    channel: int = 0,
    value: float = 1.0,
    frequency_hz: float = 9.4e8,
    blank_beams: tuple[int, ...] = (),
) -> list[Path]:
    """The images of one Stokes of a 2x2 footprint of beams a degree apart"""
    offsets = ((-0.5, -0.5), (0.5, -0.5), (-0.5, 0.5), (0.5, 0.5))
    return [
        write_beam_image(
            directory
            / f"SB1234.RACS_0000-00.beam{beam:02d}.round3.{stokes}.ch{channel:04d}-{channel + 1:04d}.image.fits",
            centre=BORESIGHT.spherical_offsets_by(east * u.deg, north * u.deg),
            stokes=stokes,
            value=value,
            frequency_hz=frequency_hz,
            blank=beam in blank_beams,
        )
        for beam, (east, north) in enumerate(offsets)
    ]


def mosaic_footprint(
    directory: Path,
    stokes_values: dict[str, float],
    boresight: SkyCoord | None = BORESIGHT,
    patchwork_options: PatchworkOptions = NUMPY,
    trim: bool = False,
    **image_kwargs,
) -> dict[str, MosaicResult]:
    """Mosaic a footprint of each Stokes, each of the value given"""
    directory.mkdir(exist_ok=True)
    return patchwork_stokes_images(
        stokes_images={
            stokes: beam_images(directory, stokes, value=value, **image_kwargs)
            for stokes, value in stokes_values.items()
        },
        stokes_base_paths={
            stokes: directory / f"field.{stokes}" for stokes in stokes_values
        },
        patchwork_options=patchwork_options,
        boresight=boresight,
        trim=trim,
    )


def test_every_stokes_is_mosaicked_under_the_linmos_names(tmp_path: Path) -> None:
    """Each Stokes gets a mosaic and a weight where linmos would put them"""
    mosaics = mosaic_footprint(tmp_path, {"i": 1.0, "q": 0.5, "u": 0.5, "v": 0.1})

    for stokes, mosaic in mosaics.items():
        assert mosaic.image_fits == tmp_path / f"field.{stokes}.linmos.fits"
        assert mosaic.weight_fits == tmp_path / f"field.{stokes}.weight.fits"
        image = fits.getdata(mosaic.image_fits)
        assert image.ndim == 4
        assert image.shape == fits.getdata(mosaic.weight_fits).shape
        assert fits.getheader(mosaic.image_fits)["CRVAL4"] == STOKES_CODES[stokes]
    # The mosaic is corrected for the primary beam, alike for every Stokes
    v_over_i = fits.getdata(mosaics["v"].image_fits) / fits.getdata(
        mosaics["i"].image_fits
    )
    assert np.nanmedian(v_over_i) == pytest.approx(0.1, abs=0.02)
    assert fits.getheader(mosaics["q"].image_fits)["POLFRAME"] == "NORTH"

    # Q and U share one weight, and each is still left with a file of its own
    assert np.array_equal(
        fits.getdata(mosaics["q"].weight_fits),
        fits.getdata(mosaics["u"].weight_fits),
        equal_nan=True,
    )
    assert not list(tmp_path.glob("*patchwork*"))


@pytest.mark.parametrize("stokes", ["i", "v"])
def test_one_stokes_is_mosaicked_on_its_own(tmp_path: Path, stokes: str) -> None:
    """The continuum and Stokes V mosaics are of one Stokes, whichever it is"""
    images = beam_images(tmp_path, stokes)

    mosaic = patchwork_images(
        images=images,
        base_path=tmp_path / "field",
        patchwork_options=NUMPY,
        boresight=BORESIGHT,
    )

    assert mosaic.image_fits == tmp_path / "field.linmos.fits"
    assert mosaic.weight_fits == tmp_path / "field.weight.fits"
    assert fits.getheader(mosaic.image_fits)["CRVAL4"] == STOKES_CODES[stokes]
    assert np.isfinite(fits.getdata(mosaic.image_fits)).any()
    assert all(image.exists() for image in images)


def test_one_stokes_can_remove_its_beam_images(tmp_path: Path) -> None:
    """Subtract-cube has no use for the beam images of an interval once mosaicked"""
    images = beam_images(tmp_path, "i")

    mosaic = patchwork_images(
        images=images,
        base_path=tmp_path / "field",
        remove_original_images=True,
        patchwork_options=NUMPY,
    )

    assert mosaic.image_fits.exists()
    assert not any(image.exists() for image in images)


def test_channels_share_one_pixel_grid(tmp_path: Path) -> None:
    """The primary beam narrows with frequency, but the grid may not, as the
    channels are stacked into one cube"""
    headers = [
        fits.getheader(
            mosaic_footprint(tmp_path / name, {"i": 1.0}, frequency_hz=frequency_hz)[
                "i"
            ].image_fits
        )
        for name, frequency_hz in (("low", 7.0e8), ("high", 1.8e9))
    ]

    assert headers[0]["NAXIS1"] == headers[1]["NAXIS1"]
    assert headers[0]["NAXIS2"] == headers[1]["NAXIS2"]
    assert WCS(headers[0]).celestial.wcs == WCS(headers[1]).celestial.wcs


def test_a_trimmed_mosaic_stops_at_the_primary_beam(tmp_path: Path) -> None:
    """A mosaic that stands alone, as an MFS one does, is not left padded out to
    the whole of every beam image"""
    whole, trimmed = (
        fits.getdata(
            mosaic_footprint(
                tmp_path / name, {"i": 1.0}, frequency_hz=1.8e9, trim=trim
            )["i"].image_fits
        )
        for name, trim in (("whole", False), ("trimmed", True))
    )

    assert trimmed.size < whole.size
    assert np.sum(np.isfinite(trimmed)) == np.sum(np.isfinite(whole))


def test_stokes_q_alone_keeps_a_weight_of_its_own(tmp_path: Path) -> None:
    """Only Q and U together share a weight, which patchwork names for itself"""
    mosaic = mosaic_footprint(tmp_path, {"q": 1.0})["q"]

    assert mosaic.weight_fits == tmp_path / "field.q.weight.fits"
    assert np.any(np.isfinite(fits.getdata(mosaic.weight_fits)))
    assert not list(tmp_path.glob("*patchwork*"))


def test_an_earlier_run_is_replaced(tmp_path: Path) -> None:
    """A task run again by dask meets what its first run left half written"""
    for name in ("field.i.linmos.fits", "field.i.patchwork.i.mosaic.fits"):
        (tmp_path / name).write_bytes(b"half written")

    mosaic = mosaic_footprint(tmp_path, {"i": 1.0})["i"]

    assert np.any(np.isfinite(fits.getdata(mosaic.image_fits)))


def test_a_blank_channel_stays_blank(tmp_path: Path) -> None:
    """A channel with every beam blanked gives a blank plane, marked as one"""
    mosaic = mosaic_footprint(tmp_path, {"i": 1.0}, blank_beams=(0, 1, 2, 3))["i"]

    assert np.all(np.isnan(fits.getdata(mosaic.image_fits)))
    assert fits.getheader(mosaic.image_fits)["BMAJ"] == 0.0


def test_a_blank_first_beam_does_not_cost_the_mosaic_its_beam(tmp_path: Path) -> None:
    """The restoring beam comes from a beam that has one, not simply the first"""
    mosaic = mosaic_footprint(tmp_path, {"i": 1.0}, blank_beams=(0,))["i"]

    assert fits.getheader(mosaic.image_fits)["BMAJ"] == pytest.approx(0.01)
    assert np.any(np.isfinite(fits.getdata(mosaic.image_fits)))


def test_q_and_u_need_the_boresight(tmp_path: Path) -> None:
    """Without the boresight nothing says what Q and U are measured from"""
    with pytest.raises(ValueError, match="Q and U"):
        mosaic_footprint(tmp_path, {"q": 1.0, "u": 0.0}, boresight=None)


def test_q_and_u_are_rotated_to_north_at_each_pixel(tmp_path: Path) -> None:
    """Q alone at the boresight's north is part U at the north of a pixel off to
    one side, and the other way about on the other side"""
    mosaics = mosaic_footprint(tmp_path, {"q": 1.0, "u": 0.0})

    u_over_q = (
        fits.getdata(mosaics["u"].image_fits).squeeze()
        / fits.getdata(mosaics["q"].image_fits).squeeze()
    )
    # The edge columns lie beyond the primary beam cutoff of every beam
    covered = u_over_q[:, np.any(np.isfinite(u_over_q), axis=0)]
    columns = np.nanmedian(covered, axis=0)
    left, middle, right = columns[:10], columns[len(columns) // 2], columns[-10:]
    assert np.all(np.abs(left) > 0.1)
    assert np.all(np.sign(left) == -np.sign(right))
    assert abs(middle) < 0.05


def test_the_default_backend_mosaics(tmp_path: Path) -> None:
    """The backend flint uses unless a strategy says otherwise"""
    images = [
        fits.getdata(
            mosaic_footprint(tmp_path / name, {"i": 1.0}, patchwork_options=options)[
                "i"
            ].image_fits
        )
        for name, options in (("numpy", NUMPY), ("default", PatchworkOptions()))
    ]

    assert np.any(np.isfinite(images[0]))
    assert np.allclose(images[0], images[1], rtol=1e-4, equal_nan=True)
