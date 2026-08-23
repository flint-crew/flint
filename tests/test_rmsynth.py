"""Plumbing tests for RM-synthesis via rm-lite"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from flint.exceptions import NotSupportedError
from flint.options import RMCleanOptions, RMSynthOptions
from flint.rmsynth import (
    FDFLabel,
    needs_rmclean,
    run_rmclean_3d,
    run_rmsynth_3d,
    write_rm_products,
)

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


def _synth_and_write(
    stokes_q_cube: Path,
    stokes_u_cube: Path,
    rmsynth_options: RMSynthOptions,
    rmclean_options: RMCleanOptions,
    cube_products: list[FDFLabel],
    moment_products: list[FDFLabel],
    output_prefix: Path,
    stokes_i_cube: Path | None = None,
    stokes_i_error_cube: Path | None = None,
) -> list[Path]:
    """Mirror of the call sequence in ``flint.prefect.flows.rmsynth_pipeline``,
    keeping these unit tests off prefect and dask. The flow itself is tested in
    ``tests/test_prefect_rmsynth_flow.py``."""
    if not cube_products and not moment_products:
        return []

    synth_results = run_rmsynth_3d(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=rmsynth_options,
        stokes_i_cube=stokes_i_cube,
        stokes_i_error_cube=stokes_i_error_cube,
    )
    clean_results = (
        run_rmclean_3d(rm_synth_results=synth_results, rmclean_options=rmclean_options)
        if needs_rmclean(cube_products=cube_products, moment_products=moment_products)
        else None
    )
    return write_rm_products(
        synth_results=synth_results,
        clean_results=clean_results,
        stokes_q_cube=stokes_q_cube,
        rmsynth_options=rmsynth_options,
        cube_products=cube_products,
        moment_products=moment_products,
        output_prefix=output_prefix,
    )


def test_rmsynth_all_products(tmp_path: Path, qu_cubes: tuple[Path, Path]) -> None:
    stokes_q_cube, stokes_u_cube = qu_cubes
    output_prefix = tmp_path / "test_field"

    output_paths = _synth_and_write(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
        rmclean_options=RMCleanOptions(),
        cube_products=["dirty", "clean", "model"],
        moment_products=["dirty", "clean", "model"],
        output_prefix=output_prefix,
    )

    # One zarr store holding all three cubes, plus three moments per label
    assert len(output_paths) == 1 + 3 * 3
    for path in output_paths:
        assert path.exists()

    assert not list(tmp_path.glob("*.fdf.*.cube.fits")), (
        "FDF cubes are zarr-only; a FITS cube means the gather path came back"
    )

    for label in ("dirty", "clean", "model"):
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

    output_paths = _synth_and_write(
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

    output_paths = _synth_and_write(
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

    output_paths = _synth_and_write(
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


def test_rmsynth_writes_fdf_cubes_to_zarr(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    stokes_q_cube, stokes_u_cube = qu_cubes
    output_prefix = tmp_path / "test_field"

    output_paths = _synth_and_write(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
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
    assert set(group.keys()) == {"dirty", "clean", "phi_arr_radm2"}
    assert group["dirty"].shape[1:] == (NY, NX)
    # Without the Faraday depth axis the store is not self-describing
    assert group["phi_arr_radm2"].shape[0] == group["dirty"].shape[0]


def test_moment_only_never_computes_a_full_cube(
    tmp_path: Path, qu_cubes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moment maps must be computed as lazy (ny, nx) reductions, never by
    gathering the (n_phi, ny, nx) FDF cube into the calling process. Gathering
    is invisible on these tiny test cubes but is tens of GB on a real mosaic."""
    import dask

    stokes_q_cube, stokes_u_cube = qu_cubes
    real_compute = dask.compute
    computed_shapes: list[tuple[int, ...]] = []

    def _spy_compute(*args, **kwargs):
        results = real_compute(*args, **kwargs)
        computed_shapes.extend(
            np.shape(result) for result in results if hasattr(result, "shape")
        )
        return results

    monkeypatch.setattr(dask, "compute", _spy_compute)

    _synth_and_write(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
        rmclean_options=RMCleanOptions(),
        cube_products=[],
        moment_products=["dirty", "clean", "model"],
        output_prefix=tmp_path / "test_field",
    )

    assert computed_shapes, "expected a batched dask.compute call"
    # Every gathered array is a 2D map; a 3D shape means an FDF cube came back.
    assert all(len(shape) == 2 for shape in computed_shapes), computed_shapes
    # 3 labels x mom0/mom1/mom2, and nothing else.
    assert len(computed_shapes) == 9


def test_rmsynth_no_products_is_noop(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    stokes_q_cube, stokes_u_cube = qu_cubes

    output_paths = _synth_and_write(
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


def test_rmsynth_rejects_compressed_cubes(tmp_path: Path) -> None:
    """A gzipped cube cannot be memmapped, so rm-lite's per-block reopens would
    each inflate the whole cube into memory."""
    with pytest.raises(NotSupportedError):
        run_rmsynth_3d(
            stokes_q_cube=tmp_path / "q.fits.gz",
            stokes_u_cube=tmp_path / "u.fits.gz",
            rmsynth_options=RMSynthOptions(),
        )


def _make_noise_only_cubes(
    tmp_path: Path, prefix: str = "noise"
) -> tuple[Path, Path, Path]:
    """Q/U/I cubes of pure noise, no source anywhere.

    The point of a noise-only cube is that everything the pipeline reports for
    it is a property of the noise handling rather than of a signal: no pixel
    should be fitted, cleaned, or given a polarised flux.
    """
    freq_hz = np.linspace(800e6, 1800e6, 40)
    rng = np.random.default_rng(20)
    shape = (freq_hz.size, 8, 8)

    wcs = WCS(naxis=3)
    wcs.wcs.ctype = ["RA---SIN", "DEC--SIN", "FREQ"]
    wcs.wcs.crval = [180.0, -30.0, freq_hz[0]]
    wcs.wcs.crpix = [shape[2] / 2, shape[1] / 2, 1]
    wcs.wcs.cdelt = [-1e-3, 1e-3, freq_hz[1] - freq_hz[0]]
    wcs.wcs.cunit = ["deg", "deg", "Hz"]
    header = wcs.to_header()

    paths = []
    for stokes in ("q", "u", "i"):
        cube = rng.normal(0.0, 1e-3, shape).astype(np.float32)
        path = tmp_path / f"{prefix}.{stokes}.fits"
        fits.writeto(path, cube, header, overwrite=True)
        paths.append(path)
    return paths[0], paths[1], paths[2]


def test_estimate_stokes_i_noise_defaults_on() -> None:
    """The Stokes I SNR cut is inert without a noise to compare against, so the
    estimate that gives it one has to be on by default -- see
    ``_warn_if_snr_cut_inert``."""
    options = RMSynthOptions()
    assert options.estimate_stokes_i_noise is True
    assert options.stokes_i_snr_cut is not None


def test_warns_when_stokes_i_snr_cut_is_inert(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A cut with no noise behind it passes every pixel, so every pixel gets a
    curve_fit. Correct, but ~1000x slower than intended, hence the warning."""
    from flint.rmsynth import _warn_if_snr_cut_inert

    caplog.set_level("WARNING")
    _warn_if_snr_cut_inert(
        RMSynthOptions(estimate_stokes_i_noise=False, stokes_i_snr_cut=5.0), None
    )
    assert "will do nothing" in caplog.text

    # Either source of a Stokes I noise makes it meaningful again, and so does
    # turning the cut off deliberately.
    for options, error_cube in (
        (RMSynthOptions(estimate_stokes_i_noise=True, stokes_i_snr_cut=5.0), None),
        (
            RMSynthOptions(estimate_stokes_i_noise=False, stokes_i_snr_cut=5.0),
            tmp_path / "i_err.fits",
        ),
        (RMSynthOptions(estimate_stokes_i_noise=False, stokes_i_snr_cut=None), None),
    ):
        caplog.clear()
        _warn_if_snr_cut_inert(options, error_cube)
        assert caplog.text == ""


def test_stokes_i_fit_on_noise_stays_finite(tmp_path: Path) -> None:
    """With the SNR cut working, a noise-only cube must come back with no
    polarised flux and nothing infinite.

    A power law fitted to a noise spectrum is unconstrained and can dip to
    ~1e-10 mid-band; Q/U divided by that is an infinite FDF and an infinite
    mom0. The cut is what stops those pixels being fitted at all.
    """
    q_cube, u_cube, i_cube = _make_noise_only_cubes(tmp_path)
    output_prefix = tmp_path / "noise_field"

    _synth_and_write(
        stokes_q_cube=q_cube,
        stokes_u_cube=u_cube,
        stokes_i_cube=i_cube,
        rmsynth_options=RMSynthOptions(
            estimate_stokes_i_noise=True, stokes_i_snr_cut=5.0
        ),
        rmclean_options=RMCleanOptions(),
        cube_products=[],
        moment_products=["dirty", "clean"],
        output_prefix=output_prefix,
    )

    for label in ("dirty", "clean"):
        mom0 = fits.getdata(Path(f"{output_prefix}.fdf.{label}.mom0.fits"))
        assert np.isfinite(mom0).all(), f"{label} mom0 has non-finite pixels"
        # Nothing clears the moment threshold, so there is no polarised flux
        assert np.allclose(mom0, 0.0), f"{label} mom0 found flux in pure noise"


def test_moment_threshold_applies_to_the_dirty_fdf(tmp_path: Path) -> None:
    """mom0 sums |FDF| over every Faraday depth, so an unthresholded off-source
    pixel integrates hundreds of noise samples into a large positive floor. The
    cut has to reach the dirty moments, not just the cleaned ones."""
    q_cube, u_cube, _ = _make_noise_only_cubes(tmp_path, prefix="thresh")

    thresholded = tmp_path / "thresholded"
    _synth_and_write(
        stokes_q_cube=q_cube,
        stokes_u_cube=u_cube,
        rmsynth_options=RMSynthOptions(moment_threshold_snr=5.0),
        rmclean_options=RMCleanOptions(),
        cube_products=[],
        moment_products=["dirty"],
        output_prefix=thresholded,
    )
    unthresholded = tmp_path / "unthresholded"
    _synth_and_write(
        stokes_q_cube=q_cube,
        stokes_u_cube=u_cube,
        rmsynth_options=RMSynthOptions(moment_threshold_snr=0.0),
        rmclean_options=RMCleanOptions(),
        cube_products=[],
        moment_products=["dirty"],
        output_prefix=unthresholded,
    )

    cut = fits.getdata(Path(f"{thresholded}.fdf.dirty.mom0.fits"))
    uncut = fits.getdata(Path(f"{unthresholded}.fdf.dirty.mom0.fits"))
    assert np.allclose(cut, 0.0), "noise survived the dirty moment threshold"
    assert np.nanmedian(uncut) > 0.0, "expected a noise floor with no threshold"


def test_stokes_i_error_cube_reaches_rm_lite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A supplied per-pixel error cube is what the SNR cut should measure
    against, so it has to arrive at rm-lite rather than being dropped."""
    captured: dict[str, object] = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after capturing the call")

    monkeypatch.setattr("flint.rmsynth.rmsynth_3d_from_fits", _spy)

    with pytest.raises(RuntimeError):
        run_rmsynth_3d(
            stokes_q_cube=tmp_path / "q.fits",
            stokes_u_cube=tmp_path / "u.fits",
            stokes_i_cube=tmp_path / "i.fits",
            stokes_i_error_cube=tmp_path / "i_err.fits",
            rmsynth_options=RMSynthOptions(),
        )

    assert captured["stokes_i_error_file"] == tmp_path / "i_err.fits"
    assert captured["stokes_i_file"] == tmp_path / "i.fits"


def test_rmsynth_rejects_compressed_stokes_i_error_cube(tmp_path: Path) -> None:
    """The error cube is read block-by-block like the others, so it is subject
    to the same no-gzip rule."""
    with pytest.raises(NotSupportedError):
        run_rmsynth_3d(
            stokes_q_cube=tmp_path / "q.fits",
            stokes_u_cube=tmp_path / "u.fits",
            stokes_i_cube=tmp_path / "i.fits",
            stokes_i_error_cube=tmp_path / "i_err.fits.gz",
            rmsynth_options=RMSynthOptions(),
        )
