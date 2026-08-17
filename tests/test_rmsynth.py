"""Plumbing tests for RM-synthesis via rm-lite. rm-lite's own synthesis
correctness is its responsibility, not flint-pol's -- these tests only check
that FITS in -> WCS/product-selection -> FITS out behaves as expected.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from flint.options import RMCleanOptions, RMSynthOptions
from flint.rmsynth import rmsynth_and_write_products

N_CHAN = 20
NY = 5
NX = 5
PHI_TRUE_RADM2 = 50.0
POL_FRACTION = 0.2


def _make_qu_cubes(tmp_path: Path) -> tuple[Path, Path]:
    """Write synthetic Stokes Q/U FITS cubes with a known Faraday depth."""
    freq_hz = np.linspace(700e6, 1300e6, N_CHAN)
    lambda_sq_m2 = (299792458.0 / freq_hz) ** 2

    rng = np.random.default_rng(0)
    angle = 2 * PHI_TRUE_RADM2 * lambda_sq_m2
    q_spec = POL_FRACTION * np.cos(angle)
    u_spec = POL_FRACTION * np.sin(angle)

    noise_sigma = 1e-3
    q_cube = np.repeat(q_spec[:, None, None], NY, axis=1).repeat(NX, axis=2)
    u_cube = np.repeat(u_spec[:, None, None], NY, axis=1).repeat(NX, axis=2)
    q_cube = (q_cube + rng.normal(0, noise_sigma, q_cube.shape)).astype(np.float32)
    u_cube = (u_cube + rng.normal(0, noise_sigma, u_cube.shape)).astype(np.float32)

    wcs = WCS(naxis=3)
    wcs.wcs.ctype = ["RA---SIN", "DEC--SIN", "FREQ"]
    wcs.wcs.crval = [180.0, -30.0, freq_hz[0]]
    wcs.wcs.crpix = [NX / 2, NY / 2, 1]
    wcs.wcs.cdelt = [-1e-3, 1e-3, freq_hz[1] - freq_hz[0]]
    wcs.wcs.cunit = ["deg", "deg", "Hz"]
    header = wcs.to_header()

    q_path = tmp_path / "stokesq.fits"
    u_path = tmp_path / "stokesu.fits"
    fits.writeto(q_path, q_cube, header, overwrite=True)
    fits.writeto(u_path, u_cube, header, overwrite=True)
    return q_path, u_path


def _make_i_cube(tmp_path: Path) -> Path:
    """Write a synthetic Stokes I FITS cube (power-law spectrum + noise)
    matching the Q/U cube's WCS."""
    freq_hz = np.linspace(700e6, 1300e6, N_CHAN)
    i_spec = (freq_hz / freq_hz[0]) ** -0.7
    rng = np.random.default_rng(1)
    i_cube = np.repeat(i_spec[:, None, None], NY, axis=1).repeat(NX, axis=2)
    i_cube = (i_cube + rng.normal(0, 1e-3, i_cube.shape)).astype(np.float32)

    wcs = WCS(naxis=3)
    wcs.wcs.ctype = ["RA---SIN", "DEC--SIN", "FREQ"]
    wcs.wcs.crval = [180.0, -30.0, freq_hz[0]]
    wcs.wcs.crpix = [NX / 2, NY / 2, 1]
    wcs.wcs.cdelt = [-1e-3, 1e-3, freq_hz[1] - freq_hz[0]]
    wcs.wcs.cunit = ["deg", "deg", "Hz"]
    header = wcs.to_header()

    i_path = tmp_path / "stokesi.fits"
    fits.writeto(i_path, i_cube, header, overwrite=True)
    return i_path


@pytest.fixture
def qu_cubes(tmp_path: Path) -> tuple[Path, Path]:
    return _make_qu_cubes(tmp_path)


def test_rmsynth_all_products(tmp_path: Path, qu_cubes: tuple[Path, Path]) -> None:
    stokes_q_cube, stokes_u_cube = qu_cubes
    output_prefix = tmp_path / "test_field"

    output_paths = rmsynth_and_write_products(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
        rmclean_options=RMCleanOptions(),
        cube_products=["dirty", "clean", "model"],
        moment_products=["dirty", "clean", "model"],
        output_prefix=output_prefix,
    )

    assert len(output_paths) == 3 + 3 * 3
    for path in output_paths:
        assert path.exists()

    for label in ("dirty", "clean", "model"):
        cube_path = Path(f"{output_prefix}.fdf.{label}.cube.fits")
        assert cube_path.exists()
        header = fits.getheader(cube_path)
        assert header["CTYPE3"] == "FDEPTH"

        for moment in ("mom0", "mom1", "mom2"):
            moment_path = Path(f"{output_prefix}.fdf.{label}.{moment}.fits")
            assert moment_path.exists()
            moment_header = fits.getheader(moment_path)
            assert moment_header["NAXIS"] == 2

    clean_mom1 = fits.getdata(Path(f"{output_prefix}.fdf.clean.mom1.fits"))
    assert np.allclose(clean_mom1, PHI_TRUE_RADM2, atol=5.0)


def test_rmsynth_dirty_only_skips_rmclean(
    tmp_path: Path, qu_cubes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    stokes_q_cube, stokes_u_cube = qu_cubes
    output_prefix = tmp_path / "test_field"

    calls = []
    monkeypatch.setattr(
        "flint.rmsynth.run_rmclean_3d", lambda *args, **kwargs: calls.append(1)
    )

    output_paths = rmsynth_and_write_products(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
        rmclean_options=RMCleanOptions(),
        cube_products=["dirty"],
        moment_products=[],
        output_prefix=output_prefix,
    )

    assert calls == []
    assert len(output_paths) == 1
    assert output_paths[0].exists()
    assert not list(tmp_path.glob("*.mom*.fits"))


def test_rmsynth_with_stokes_i_writes_fit_maps(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    stokes_q_cube, stokes_u_cube = qu_cubes
    stokes_i_cube = _make_i_cube(tmp_path)
    output_prefix = tmp_path / "test_field"

    output_paths = rmsynth_and_write_products(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        stokes_i_cube=stokes_i_cube,
        rmsynth_options=RMSynthOptions(estimate_stokes_i_noise=True),
        rmclean_options=RMCleanOptions(),
        cube_products=[],
        moment_products=["dirty"],
        output_prefix=output_prefix,
    )

    for suffix in ("ref_flux", "alpha", "model_order"):
        fit_map_path = Path(f"{output_prefix}.stokesi.{suffix}.fits")
        assert fit_map_path in output_paths
        assert fit_map_path.exists()
        assert fits.getheader(fit_map_path)["NAXIS"] == 2

    # alpha_error is only produced when rm-lite actually has a Stokes I noise
    # estimate to propagate -- assert we never crash either way, and if it
    # was produced, it's a valid written 2D map.
    alpha_error_path = Path(f"{output_prefix}.stokesi.alpha_error.fits")
    if alpha_error_path in output_paths:
        assert alpha_error_path.exists()
        assert fits.getheader(alpha_error_path)["NAXIS"] == 2


def test_rmsynth_debias_moments_runs(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    stokes_q_cube, stokes_u_cube = qu_cubes
    output_prefix = tmp_path / "test_field"

    output_paths = rmsynth_and_write_products(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(debias_moments=True),
        rmclean_options=RMCleanOptions(),
        cube_products=[],
        moment_products=["clean"],
        output_prefix=output_prefix,
    )

    mom0_path = Path(f"{output_prefix}.fdf.clean.mom0.fits")
    debiased_mom0_path = Path(f"{output_prefix}.fdf.clean.mom0.debiased.fits")
    assert mom0_path in output_paths
    assert mom0_path.exists()
    assert debiased_mom0_path in output_paths
    assert debiased_mom0_path.exists()
    assert len(output_paths) == 6


def test_rmsynth_write_fdfs_to_zarr(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    stokes_q_cube, stokes_u_cube = qu_cubes
    output_prefix = tmp_path / "test_field"

    output_paths = rmsynth_and_write_products(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(write_fdfs_to_zarr=True),
        rmclean_options=RMCleanOptions(),
        cube_products=["dirty", "clean"],
        moment_products=["clean"],
        output_prefix=output_prefix,
    )

    zarr_store = Path(f"{output_prefix}.fdf.zarr")
    assert zarr_store in output_paths
    assert not list(tmp_path.glob("*.fdf.*.cube.fits"))

    import zarr

    group = zarr.open(str(zarr_store), mode="r")
    assert set(group.keys()) == {"dirty", "clean"}
    assert group["dirty"].shape[1:] == (NY, NX)


def test_rmsynth_no_products_is_noop(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    stokes_q_cube, stokes_u_cube = qu_cubes

    output_paths = rmsynth_and_write_products(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
        rmclean_options=RMCleanOptions(),
        cube_products=[],
        moment_products=[],
        output_prefix=tmp_path / "test_field",
    )

    assert output_paths == []
    assert not list(tmp_path.glob("*.fits")) or all(
        p in (stokes_q_cube, stokes_u_cube) for p in tmp_path.glob("*.fits")
    )
