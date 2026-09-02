"""Plumbing tests for RM-synthesis via rm-lite"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS
from pydantic import ValidationError

from flint.exceptions import NotSupportedError
from flint.options import (
    RMCleanOptions,
    RMSynthFieldOptions,
    RMSynthOptions,
    StokesCubes,
    StokesErrorCubes,
    StokesNoiseCubes,
    StokesWeightCubes,
)
from flint.rmsynth import (
    FDFLabel,
    RMSynth3DResults,
    _snr_threshold,
    needs_rmclean,
    run_rmclean_3d,
    run_rmsynth_3d,
    write_rm_products,
    write_stokes_i_coeff_maps_to_fits,
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


def _make_weight_cube(
    tmp_path: Path,
    shape: tuple[int, int, int],
    freq_hz: np.ndarray,
    sigma: float,
    name: str,
) -> Path:
    """Write a constant per-channel weight cube (1/sigma**2) for use as a
    ``*_weight_cube`` with ``noise_files_are_weight=True``."""
    n_chan, ny, nx = shape
    weight_cube = np.full(shape, 1.0 / sigma**2, dtype=np.float32)

    wcs = WCS(naxis=3)
    wcs.wcs.ctype = ["RA---SIN", "DEC--SIN", "FREQ"]
    wcs.wcs.crval = [180.0, -30.0, freq_hz[0]]
    wcs.wcs.crpix = [nx / 2, ny / 2, 1]
    wcs.wcs.cdelt = [-1e-3, 1e-3, freq_hz[1] - freq_hz[0]]
    wcs.wcs.cunit = ["deg", "deg", "Hz"]
    header = wcs.to_header()

    weight_path = tmp_path / f"{name}.weight.fits"
    fits.writeto(weight_path, weight_cube, header, overwrite=True)
    return weight_path


def _make_i_weight_cube(tmp_path: Path, sigma: float = 1e-3) -> Path:
    freq_hz = np.linspace(700e6, 1300e6, N_CHAN)
    return _make_weight_cube(tmp_path, (N_CHAN, NY, NX), freq_hz, sigma, "stokesi")


@pytest.fixture
def qu_cubes(tmp_path: Path) -> tuple[Path, Path]:
    return _make_qu_cubes(tmp_path)


def _run_rmsynth_3d(
    stokes_q_cube: Path,
    stokes_u_cube: Path,
    rmsynth_options: RMSynthOptions,
    stokes_i_cube: Path | None = None,
    stokes_q_weight_cube: Path | None = None,
    stokes_u_weight_cube: Path | None = None,
    stokes_i_weight_cube: Path | None = None,
    stokes_q_noise_cube: Path | None = None,
    stokes_u_noise_cube: Path | None = None,
    stokes_i_noise_cube: Path | None = None,
) -> RMSynth3DResults:
    """``run_rmsynth_3d`` from a flat set of paths.

    These tests are about what rm-synthesis does, not how its arguments are
    grouped, so the containers are built here rather than at ninety call sites.
    ``StokesErrorCubes`` itself is covered by its own tests below.
    """
    error_cubes: StokesErrorCubes | None = None
    if stokes_q_noise_cube is not None:
        error_cubes = StokesNoiseCubes(
            q=stokes_q_noise_cube, u=stokes_u_noise_cube, i=stokes_i_noise_cube
        )
    elif stokes_q_weight_cube is not None:
        error_cubes = StokesWeightCubes(
            q=stokes_q_weight_cube, u=stokes_u_weight_cube, i=stokes_i_weight_cube
        )
    return run_rmsynth_3d(
        stokes_cubes=StokesCubes(q=stokes_q_cube, u=stokes_u_cube, i=stokes_i_cube),
        rmsynth_options=rmsynth_options,
        error_cubes=error_cubes,
    )


def _synth_and_write(
    stokes_q_cube: Path,
    stokes_u_cube: Path,
    rmsynth_options: RMSynthOptions,
    rmclean_options: RMCleanOptions,
    cube_products: list[FDFLabel],
    moment_products: list[FDFLabel],
    output_prefix: Path,
    stokes_i_cube: Path | None = None,
    stokes_i_weight_cube: Path | None = None,
    stokes_q_weight_cube: Path | None = None,
    stokes_u_weight_cube: Path | None = None,
    peak_products: list[FDFLabel] = [],
) -> list[Path]:
    if not cube_products and not moment_products and not peak_products:
        return []

    synth_results = _run_rmsynth_3d(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=rmsynth_options,
        stokes_i_cube=stokes_i_cube,
        stokes_i_weight_cube=stokes_i_weight_cube,
        stokes_q_weight_cube=stokes_q_weight_cube,
        stokes_u_weight_cube=stokes_u_weight_cube,
    )
    clean_results = (
        run_rmclean_3d(rm_synth_results=synth_results, rmclean_options=rmclean_options)
        if needs_rmclean(
            cube_products=cube_products,
            moment_products=moment_products,
            peak_products=peak_products,
        )
        else None
    )
    return write_rm_products(
        synth_results=synth_results,
        clean_results=clean_results,
        stokes_q_cube=stokes_q_cube,
        rmsynth_options=rmsynth_options,
        rmclean_options=rmclean_options,
        cube_products=cube_products,
        moment_products=moment_products,
        peak_products=peak_products,
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

    # One zarr store holding all three cubes, three moments per label, and the
    # CLEAN iteration count that every RM-CLEAN run writes
    assert len(output_paths) == 1 + 3 * 3 + 1
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
        rmsynth_options=RMSynthOptions(),
        rmclean_options=RMCleanOptions(),
        cube_products=[],
        moment_products=["dirty"],
        output_prefix=output_prefix,
        stokes_i_weight_cube=_make_i_weight_cube(tmp_path),
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

    # A spectral index or a reference flux is unreadable without the frequency it
    # is defined at and the functional form it belongs to.
    header = fits.getheader(Path(f"{output_prefix}.stokesi.alpha.fits"))
    assert header["FITFUNC"] == "log"
    assert 700e6 < header["REFFREQ"] < 1300e6


@pytest.mark.parametrize(
    ("fit_function", "expected_names"),
    [("log", ("flux", "alpha", "beta")), ("linear", ("c0", "c1", "c2"))],
)
def test_rmsynth_writes_a_named_map_per_stokes_i_model_term(
    tmp_path: Path,
    qu_cubes: tuple[Path, Path],
    fit_function: str,
    expected_names: tuple[str, ...],
) -> None:
    """Each fitted Stokes I model term gets its own named map, since the names
    are what make the terms usable, and they differ by fit function."""
    stokes_q_cube, stokes_u_cube = qu_cubes
    stokes_i_cube = _make_i_cube(tmp_path)
    output_prefix = tmp_path / "test_field"

    output_paths = _synth_and_write(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        stokes_i_cube=stokes_i_cube,
        rmsynth_options=RMSynthOptions(fit_function=fit_function),
        rmclean_options=RMCleanOptions(),
        cube_products=[],
        moment_products=["dirty"],
        output_prefix=output_prefix,
        stokes_i_weight_cube=_make_i_weight_cube(tmp_path),
    )

    for index, name in enumerate(expected_names):
        term_path = Path(f"{output_prefix}.stokesi.coeff.{name}.fits")
        assert term_path in output_paths
        header = fits.getheader(term_path)
        assert header["NAXIS"] == 2
        assert header["SICOEFF"] == name
        assert header["SICOEFFI"] == index
        assert header["FITFUNC"] == fit_function
        assert 700e6 < header["REFFREQ"] < 1300e6

        error_path = Path(f"{output_prefix}.stokesi.coeff.{name}_error.fits")
        assert error_path in output_paths
        assert fits.getheader(error_path)["NAXIS"] == 2

    # Under the log fit, term 1 *is* the spectral index, so it has to match the
    # standalone alpha map. Not so for the linear fit, where alpha is
    # d ln I / d ln nu at the reference frequency rather than any single c_i.
    if fit_function == "log":
        assert np.allclose(
            fits.getdata(Path(f"{output_prefix}.stokesi.alpha.fits")),
            fits.getdata(Path(f"{output_prefix}.stokesi.coeff.alpha.fits")),
            equal_nan=True,
        )


def test_stokes_i_coeff_maps_without_an_error_cube(tmp_path: Path) -> None:
    """The error cube is optional, so the term maps are written without it."""
    reference_header = fits.getheader(_make_i_cube(tmp_path))
    output_prefix = tmp_path / "test_field"

    output_paths = write_stokes_i_coeff_maps_to_fits(
        coeff_cube=np.zeros((2, NY, NX)),
        coeff_names=("flux", "alpha"),
        reference_header=reference_header,
        output_prefix=output_prefix,
    )

    assert output_paths == [
        Path(f"{output_prefix}.stokesi.coeff.flux.fits"),
        Path(f"{output_prefix}.stokesi.coeff.alpha.fits"),
    ]
    assert not list(tmp_path.glob("*_error.fits"))
    # Nothing told us what the terms are referenced to, so nothing is claimed.
    header = fits.getheader(output_paths[0])
    assert "REFFREQ" not in header
    assert "FITFUNC" not in header


def test_stokes_i_coeff_maps_refuse_a_name_and_plane_mismatch(tmp_path: Path) -> None:
    """Writing the planes under the wrong names would be worse than not writing
    them, so a disagreement between the two is refused rather than truncated."""
    with pytest.raises(ValueError, match="named 2 Stokes I model terms"):
        write_stokes_i_coeff_maps_to_fits(
            coeff_cube=np.zeros((3, NY, NX)),
            coeff_names=("flux", "alpha"),
            reference_header=fits.getheader(_make_i_cube(tmp_path)),
            output_prefix=tmp_path / "test_field",
        )


def test_unnamed_stokes_i_model_terms_are_skipped_with_a_warning(
    tmp_path: Path, qu_cubes: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """rm-lite names the terms whenever it returns them, so this state takes an
    rm-lite regression to reach. Unnamed planes are not worth writing, but they
    are not worth losing the rest of the products over either.
    """
    stokes_q_cube, stokes_u_cube = qu_cubes
    rmsynth_options = RMSynthOptions()
    synth_results = _run_rmsynth_3d(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=rmsynth_options,
        stokes_i_cube=_make_i_cube(tmp_path),
        stokes_i_weight_cube=_make_i_weight_cube(tmp_path),
    )
    assert synth_results.stokes_i_coeff_names is not None

    output_paths = write_rm_products(
        synth_results=synth_results._replace(stokes_i_coeff_names=None),
        clean_results=None,
        stokes_q_cube=stokes_q_cube,
        rmsynth_options=rmsynth_options,
        rmclean_options=RMCleanOptions(),
        cube_products=[],
        moment_products=["dirty"],
        peak_products=[],
        output_prefix=tmp_path / "test_field",
    )

    assert not any("coeff" in path.name for path in output_paths)
    assert any("no names for them" in record.message for record in caplog.records)
    # The maps that are still nameable are written regardless.
    assert any(path.name.endswith("stokesi.alpha.fits") for path in output_paths)


def test_stokes_i_model_rebuilds_from_the_written_term_maps(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    """The point of writing the terms is that the model can be evaluated at any
    frequency from them alone. Rebuild it from the written maps and their
    headers, and check it against the model cube rm-lite fitted.

    ``fit_order=-3`` lets the AIC pick the order, so this also pins the
    zero-versus-NaN convention: our Stokes I spectrum is a pure power law, so the
    higher terms are dropped, and a dropped term has to come back as zero -- it
    contributes nothing to the model -- rather than NaN, which would poison the
    sum below.
    """
    stokes_q_cube, stokes_u_cube = qu_cubes
    stokes_i_cube = _make_i_cube(tmp_path)
    output_prefix = tmp_path / "test_field"
    rmsynth_options = RMSynthOptions(fit_order=-3)

    synth_results = _run_rmsynth_3d(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=rmsynth_options,
        stokes_i_cube=stokes_i_cube,
        stokes_i_weight_cube=_make_i_weight_cube(tmp_path),
    )
    write_rm_products(
        synth_results=synth_results,
        clean_results=None,
        stokes_q_cube=stokes_q_cube,
        rmsynth_options=rmsynth_options,
        rmclean_options=RMCleanOptions(),
        cube_products=[],
        moment_products=["dirty"],
        peak_products=[],
        output_prefix=output_prefix,
    )

    names = synth_results.stokes_i_coeff_names
    assert names is not None
    terms = np.stack(
        [
            fits.getdata(Path(f"{output_prefix}.stokesi.coeff.{name}.fits"))
            for name in names
        ]
    )
    header = fits.getheader(Path(f"{output_prefix}.stokesi.coeff.{names[0]}.fits"))
    assert header["FITFUNC"] == "log"

    # A dropped term is zero, not NaN, and model_order says how many were fitted.
    order_map = fits.getdata(Path(f"{output_prefix}.stokesi.model_order.fits"))
    dropped = terms[int(order_map.max()) + 1 :]
    assert dropped.size, "expected the AIC to drop at least one term here"
    assert np.all(dropped == 0.0)

    freq_hz = np.linspace(700e6, 1300e6, N_CHAN)
    log_freq_ratio = np.log10(freq_hz / header["REFFREQ"])[:, None, None]
    exponent = sum(
        terms[power] * log_freq_ratio**power for power in range(1, len(names))
    )
    rebuilt = terms[0] * 10.0**exponent

    assert np.allclose(
        rebuilt, np.asarray(synth_results.stokes_i_model_cube), rtol=1e-5
    )


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
    # Three moments, three debiased, plus the CLEAN iteration count
    assert len(output_paths) == 6 + 1


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
    # Nine moment maps plus the CLEAN iteration count, all still (ny, nx)
    assert len(computed_shapes) == 9 + 1


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
        _run_rmsynth_3d(
            stokes_q_cube=tmp_path / "q.fits.gz",
            stokes_u_cube=tmp_path / "u.fits.gz",
            rmsynth_options=RMSynthOptions(),
        )


def test_rmsynth_rejects_a_compressed_weight_cube(tmp_path: Path) -> None:
    """The weight cubes are read block-by-block just like the Stokes cubes, so a
    gzipped one inflates the whole cube per read in exactly the same way."""
    stokes_q_cube, stokes_u_cube = _make_qu_cubes(tmp_path)
    for weights in (
        {"stokes_q_weight_cube": tmp_path / "q.weight.fits.gz"},
        {"stokes_u_weight_cube": tmp_path / "u.weight.fits.gz"},
        {"stokes_i_weight_cube": tmp_path / "i.weight.fits.gz"},
    ):
        with pytest.raises(NotSupportedError):
            _run_rmsynth_3d(
                stokes_q_cube=stokes_q_cube,
                stokes_u_cube=stokes_u_cube,
                stokes_i_cube=_make_i_cube(tmp_path),
                rmsynth_options=RMSynthOptions(),
                **weights,
            )


def test_stokes_i_noise_is_estimated_when_no_weight_cube_is_given(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    """The linmos weight cubes are the preferred Stokes I error, but they only
    exist when the cubes came from the polarisation pipeline. rm-lite refuses a
    ``stokes_i_snr_cut`` with no error to score against, so a Stokes I cube on its
    own has to fall back to the per-channel estimate rather than raise."""
    assert RMSynthOptions().estimate_stokes_i_noise is True
    assert RMSynthOptions().stokes_i_snr_cut is not None

    stokes_q_cube, stokes_u_cube = qu_cubes
    output_prefix = tmp_path / "no_i_weights"

    output_paths = _synth_and_write(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        stokes_i_cube=_make_i_cube(tmp_path),
        rmsynth_options=RMSynthOptions(),
        rmclean_options=RMCleanOptions(),
        cube_products=[],
        moment_products=["dirty"],
        output_prefix=output_prefix,
        stokes_i_weight_cube=None,
    )

    alpha_path = Path(f"{output_prefix}.stokesi.alpha.fits")
    assert alpha_path in output_paths
    assert np.all(np.isfinite(fits.getdata(alpha_path)))


def test_rmsynth_options_reach_rm_lite(
    tmp_path: Path, qu_cubes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every RMSynthOptions field is there to change what rm-lite does, so one
    that is never passed on is a silently ignored setting rather than a default."""
    import flint.rmsynth as rmsynth_mod

    captured: dict[str, object] = {}

    def _capture(**kwargs: object) -> None:
        captured.update(kwargs)
        msg = "stop before synthesising"
        raise NotSupportedError(msg)

    monkeypatch.setattr(rmsynth_mod, "rmsynth_3d_from_fits", _capture)

    stokes_q_cube, stokes_u_cube = qu_cubes
    rmsynth_options = RMSynthOptions(per_pixel_rmsf=True, estimate_stokes_i_noise=False)
    with pytest.raises(NotSupportedError, match="stop before synthesising"):
        _run_rmsynth_3d(
            stokes_q_cube=stokes_q_cube,
            stokes_u_cube=stokes_u_cube,
            stokes_i_cube=_make_i_cube(tmp_path),
            rmsynth_options=rmsynth_options,
        )

    assert captured["per_pixel_rmsf"] is True
    assert captured["estimate_stokes_i_noise"] is False


def _within_cutoff(blank_outside: float) -> np.ndarray:
    """The (ny, nx) mask ``_make_linmos_weight_cube`` leaves non-zero."""
    yy, xx = np.mgrid[0:NY, 0:NX]
    return np.hypot(yy - (NY - 1) / 2, xx - (NX - 1) / 2) <= blank_outside


def _make_linmos_weight_cube(
    tmp_path: Path,
    name: str,
    blank_outside: float | None = None,
    blank_channels: tuple[int, ...] = (),
    taper_per_channel: bool = False,
) -> Path:
    """A weight cube shaped like the one linmos writes: a primary-beam taper,
    zero outside the ``LinmosOptions.cutoff``, and an all-zero plane for any
    channel that was flagged everywhere.

    ``taper_per_channel`` narrows the taper with frequency, as a real primary
    beam does. That is what stops pixels sharing one channel weighting, so it is
    the case that earns the per-pixel RMSF; the default keeps the taper flat in
    frequency so the shared spectrum still describes every pixel.
    """
    freq_hz = np.linspace(700e6, 1300e6, N_CHAN)
    yy, xx = np.mgrid[0:NY, 0:NX]
    radius = np.hypot(yy - (NY - 1) / 2, xx - (NX - 1) / 2)

    plane = np.exp(-(radius**2) / 2.0)
    if blank_outside is not None:
        plane = np.where(radius > blank_outside, 0.0, plane)

    if taper_per_channel:
        # Beam width goes as 1/frequency, so each channel tapers differently
        scale = (freq_hz / freq_hz[0]) ** 2
        cube = np.exp(-(radius[None] ** 2) * scale[:, None, None] / 2.0)
        if blank_outside is not None:
            cube = np.where(radius[None] > blank_outside, 0.0, cube)
        cube = (cube / 1e-3**2).astype(np.float32)
    else:
        cube = (plane[None] / 1e-3**2).repeat(N_CHAN, axis=0).astype(np.float32)
    for channel in blank_channels:
        cube[channel] = 0.0

    wcs = WCS(naxis=3)
    wcs.wcs.ctype = ["RA---SIN", "DEC--SIN", "FREQ"]
    wcs.wcs.crval = [180.0, -30.0, freq_hz[0]]
    wcs.wcs.crpix = [NX / 2, NY / 2, 1]
    wcs.wcs.cdelt = [-1e-3, 1e-3, freq_hz[1] - freq_hz[0]]
    wcs.wcs.cunit = ["deg", "deg", "Hz"]

    path = tmp_path / f"{name}.weight.fits"
    fits.writeto(path, cube, wcs.to_header(), overwrite=True)
    return path


def test_one_linmos_weight_cube_is_refused(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    """rm-lite builds the weights from Q and U together, so a lone cube is a
    wiring mistake it refuses rather than half-applies. Pinned here because the
    racs-all flow passes the pair positionally and a silent drop would look like
    a linmos-weighted run that never was."""
    stokes_q_cube, stokes_u_cube = qu_cubes

    with pytest.raises(ValueError, match="[Mm]ust pass both"):
        _run_rmsynth_3d(
            stokes_q_cube=stokes_q_cube,
            stokes_u_cube=stokes_u_cube,
            rmsynth_options=RMSynthOptions(),
            stokes_q_weight_cube=_make_linmos_weight_cube(
                tmp_path, "q", blank_outside=1.5
            ),
        )


def test_rmsynth_with_linmos_weights_stays_cleanable(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    """The whole point of the weight cubes is the run that follows them.

    A linmos weight cube is dominated by the primary-beam taper, so pixels do
    not share one channel weighting and rm-lite gives each its own noise and
    RMSF. That makes the theoretical noise an (ny, nx) map rather than a scalar,
    and RM-CLEAN scales its mask and threshold by it -- so this is the run that
    catches a shape or laziness mismatch between the two.
    """
    stokes_q_cube, stokes_u_cube = qu_cubes

    synth_results = _run_rmsynth_3d(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
        stokes_q_weight_cube=_make_linmos_weight_cube(tmp_path, "q", blank_outside=1.5),
        stokes_u_weight_cube=_make_linmos_weight_cube(tmp_path, "u", blank_outside=1.5),
    )

    # Per-pixel weights, so a map over the sky rather than one number for it
    fdf_error_noise = np.asarray(synth_results.theoretical_noise.fdf_error_noise)
    assert fdf_error_noise.shape == (NY, NX)
    assert np.all(fdf_error_noise[np.isfinite(fdf_error_noise)] > 0)

    clean_results = run_rmclean_3d(
        rm_synth_results=synth_results, rmclean_options=RMCleanOptions()
    )
    clean_cube = np.asarray(clean_results.clean_fdf_cube)

    # Per-pixel weights mean a pixel linmos blanked has no weight of its own and
    # so no FDF, rather than borrowing the rest of the field's. Blank there,
    # finite everywhere the cutoff kept.
    inside_cutoff = _within_cutoff(1.5)
    assert np.all(np.isfinite(clean_cube[:, inside_cutoff])), (
        "pixels inside the linmos cutoff have data and must not come back blank"
    )
    assert np.all(np.isnan(clean_cube[:, ~inside_cutoff])), (
        "pixels linmos blanked carry no weight, so they must stay blank"
    )

    phi_arr_radm2 = np.asarray(synth_results.phi_arr_radm2)
    peak_phi = phi_arr_radm2[np.abs(clean_cube[:, inside_cutoff]).mean(axis=1).argmax()]
    assert peak_phi == pytest.approx(PHI_TRUE_RADM2, abs=5.0)


def test_linmos_weights_give_each_pixel_its_own_rmsf(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    """A per-pixel RMSF is the costly half of per-pixel weights, so flint should
    not be surprised into it. rm-lite turns it on by itself whenever pixels
    weight the channels differently -- which is what a frequency-dependent
    primary beam does -- and the linmos cubes are exactly that case."""
    stokes_q_cube, stokes_u_cube = qu_cubes

    synth_results = _run_rmsynth_3d(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
        stokes_q_weight_cube=_make_linmos_weight_cube(
            tmp_path, "q", blank_outside=1.5, taper_per_channel=True
        ),
        stokes_u_weight_cube=_make_linmos_weight_cube(
            tmp_path, "u", blank_outside=1.5, taper_per_channel=True
        ),
    )

    assert synth_results.rmsf_cube is not None, (
        "pixels weighting channels differently must get the per-pixel RMSF cube"
    )
    rmsf_cube = np.asarray(synth_results.rmsf_cube)
    assert rmsf_cube.shape[-2:] == (NY, NX)

    # RM-CLEAN has to consume that cube rather than the shared spectrum
    clean_results = run_rmclean_3d(
        rm_synth_results=synth_results, rmclean_options=RMCleanOptions()
    )
    assert np.asarray(clean_results.clean_fdf_cube).shape[-2:] == (NY, NX)


def test_channels_linmos_blanked_everywhere_drop_out(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    """An all-zero weight plane is linmos saying that channel was flagged across
    the whole field. It has to leave the synthesis rather than divide into it:
    zero weight is a channel that contributes nothing, not one whose 1/0 poisons
    every pixel that would have used it."""
    stokes_q_cube, stokes_u_cube = qu_cubes
    blanked_channels = (0, 5, N_CHAN - 1)

    synth_results = _run_rmsynth_3d(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
        stokes_q_weight_cube=_make_linmos_weight_cube(
            tmp_path, "q", blank_outside=1.5, blank_channels=blanked_channels
        ),
        stokes_u_weight_cube=_make_linmos_weight_cube(
            tmp_path, "u", blank_outside=1.5, blank_channels=blanked_channels
        ),
    )
    clean_results = run_rmclean_3d(
        rm_synth_results=synth_results, rmclean_options=RMCleanOptions()
    )

    inside_cutoff = _within_cutoff(1.5)
    clean_cube = np.asarray(clean_results.clean_fdf_cube)
    noise = np.asarray(synth_results.theoretical_noise.fdf_error_noise)

    assert np.all(np.isfinite(clean_cube[:, inside_cutoff]))
    assert np.all(np.isfinite(noise[inside_cutoff]))

    phi_arr_radm2 = np.asarray(synth_results.phi_arr_radm2)
    peak_phi = phi_arr_radm2[np.abs(clean_cube[:, inside_cutoff]).mean(axis=1).argmax()]
    assert peak_phi == pytest.approx(PHI_TRUE_RADM2, abs=5.0), (
        "losing three channels must not move the recovered Faraday depth"
    )


def test_linmos_weights_through_to_the_moment_maps(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    """Weights plus moment maps is what the racs-all flow actually runs, and it
    is where the per-pixel shapes have to agree: the moment threshold is
    ``moment_threshold_snr * theoretical_noise``, now an (ny, nx) map, while the
    RMSF FWHM it is applied alongside stays a scalar. A mismatch between those
    two would not raise -- it would broadcast into the wrong threshold per pixel
    and quietly reshape every moment map."""
    stokes_q_cube, stokes_u_cube = qu_cubes
    output_prefix = tmp_path / "field"

    output_paths = _synth_and_write(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
        rmclean_options=RMCleanOptions(),
        cube_products=[],
        moment_products=["clean"],
        output_prefix=output_prefix,
        stokes_q_weight_cube=_make_linmos_weight_cube(
            tmp_path, "q", blank_outside=1.5, taper_per_channel=True
        ),
        stokes_u_weight_cube=_make_linmos_weight_cube(
            tmp_path, "u", blank_outside=1.5, taper_per_channel=True
        ),
    )

    assert {path.name for path in output_paths} == {
        f"{output_prefix.name}.fdf.clean.{moment}.fits"
        for moment in ("mom0", "mom1", "mom2")
    } | {f"{output_prefix.name}.fdf.clean.niter.fits"}

    mom1 = fits.getdata(Path(f"{output_prefix}.fdf.clean.mom1.fits"))
    inside_cutoff = _within_cutoff(1.5)
    assert np.allclose(mom1[inside_cutoff], PHI_TRUE_RADM2, atol=5.0), (
        "the per-pixel noise map must threshold each pixel, not reshape the field"
    )
    assert np.all(np.isnan(mom1[~inside_cutoff])), (
        "pixels linmos blanked have no weight, so they get no moment"
    )


def _make_noise_only_cubes(
    tmp_path: Path, prefix: str = "noise"
) -> tuple[Path, Path, Path, Path]:
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
    return (
        paths[0],
        paths[1],
        paths[2],
        _make_weight_cube(tmp_path, shape, freq_hz, sigma=1e-3, name=f"{prefix}.i"),
    )


def test_every_fdf_can_have_cubes_moments_and_peaks(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    """The three product kinds are independent of the three FDFs, so all nine
    combinations have to be reachable -- a peak map off the dirty FDF included,
    which is the one that needs no RM-CLEAN at all."""
    stokes_q_cube, stokes_u_cube = qu_cubes
    labels: list[FDFLabel] = ["dirty", "clean", "model"]
    output_prefix = tmp_path / "field"

    output_paths = _synth_and_write(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
        rmclean_options=RMCleanOptions(),
        cube_products=labels,
        moment_products=labels,
        peak_products=labels,
        output_prefix=output_prefix,
    )
    names = {path.name for path in output_paths}

    # One zarr store holds every cube; moments and peaks are a file each
    assert f"{output_prefix.name}.fdf.zarr" in names
    for label in labels:
        assert sum(f".{label}.mom" in name for name in names) == 3, label
        assert sum(f".{label}.peak_" in name for name in names) == 9, label

    # The peak Faraday depth is the recoverable truth in all three
    for label in labels:
        peak_rm = fits.getdata(Path(f"{output_prefix}.fdf.{label}.peak_rm.fits"))
        assert np.nanmedian(peak_rm) == pytest.approx(PHI_TRUE_RADM2, abs=5.0), label


def test_the_peak_and_moment_snr_cuts_are_independent(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    """One cut used to serve both, so a noise estimate that blanked the moments
    took the peaks with it and the pair looked like a broken FDF rather than an
    over-aggressive cut. They are separate options now, and each has to bite on
    its own products only.
    """
    stokes_q_cube, stokes_u_cube = qu_cubes

    def peak_pi_and_mom0(prefix: str, **snrs: float) -> tuple[np.ndarray, np.ndarray]:
        output_prefix = tmp_path / prefix
        _synth_and_write(
            stokes_q_cube=stokes_q_cube,
            stokes_u_cube=stokes_u_cube,
            rmsynth_options=RMSynthOptions(),
            rmclean_options=RMCleanOptions(**snrs),
            cube_products=[],
            moment_products=["dirty"],
            peak_products=["dirty"],
            output_prefix=output_prefix,
        )
        return (
            fits.getdata(Path(f"{output_prefix}.fdf.dirty.peak_pi.fits")),
            fits.getdata(Path(f"{output_prefix}.fdf.dirty.mom0.fits")),
        )

    # A cut nothing can clear on one side leaves the other untouched. mom0 is
    # nansum, so a fully cut spectrum reads 0 rather than NaN; a cut peak is NaN
    peak_pi, mom0 = peak_pi_and_mom0("moments_cut", moment_threshold_snr=1e6)
    assert np.all(np.isfinite(peak_pi)), "the moment cut must not reach the peaks"
    assert np.all(mom0 == 0.0)

    peak_pi, mom0 = peak_pi_and_mom0("peaks_cut", peak_threshold_snr=1e6)
    assert np.all(np.isnan(peak_pi))
    assert np.all(mom0 > 0.0), "the peak cut must not reach the moments"


def test_no_peak_cut_by_default_even_where_a_pixel_has_no_weight(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    """``peak_threshold_snr`` defaults to 0, meaning no cut at all. It cannot be
    applied as ``0 * noise``: the theoretical noise is inf for a pixel linmos
    blanked, ``0 * inf`` is NaN, and every comparison against NaN is False -- so
    the cut meant to pass everything would instead blank the whole map."""
    assert RMCleanOptions().peak_threshold_snr == 0.0
    assert _snr_threshold(0.0, np.float64(np.inf)) is None
    assert _snr_threshold(0.0, np.array([1e-5, np.inf])) is None
    assert _snr_threshold(5.0, np.float64(2.0)) == 10.0

    stokes_q_cube, stokes_u_cube = qu_cubes
    output_prefix = tmp_path / "default_cut"
    _synth_and_write(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
        rmclean_options=RMCleanOptions(),
        cube_products=[],
        moment_products=[],
        peak_products=["dirty"],
        output_prefix=output_prefix,
        stokes_q_weight_cube=_make_linmos_weight_cube(tmp_path, "q", blank_outside=1.5),
        stokes_u_weight_cube=_make_linmos_weight_cube(tmp_path, "u", blank_outside=1.5),
    )

    peak_pi = fits.getdata(Path(f"{output_prefix}.fdf.dirty.peak_pi.fits"))
    inside_cutoff = _within_cutoff(1.5)
    assert np.all(np.isfinite(peak_pi[inside_cutoff])), (
        "no cut was asked for, so every pixel carrying weight keeps its peak"
    )
    assert np.all(np.isnan(peak_pi[~inside_cutoff])), (
        "a pixel linmos blanked has no FDF to peak, cut or no cut"
    )


def test_dirty_peaks_alone_do_not_run_rmclean(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    """Peaks off the dirty FDF are measured from the synthesis output, so asking
    for only those must not drag RM-CLEAN in -- it is the expensive half of the
    stage. The iteration-count map is the tell: it only exists if CLEAN ran."""
    assert (
        needs_rmclean(cube_products=[], moment_products=[], peak_products=["dirty"])
        is False
    )
    assert (
        needs_rmclean(cube_products=[], moment_products=[], peak_products=["clean"])
        is True
    )
    assert (
        needs_rmclean(cube_products=[], moment_products=[], peak_products=["model"])
        is True
    )

    stokes_q_cube, stokes_u_cube = qu_cubes
    output_prefix = tmp_path / "field"
    output_paths = _synth_and_write(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
        rmclean_options=RMCleanOptions(),
        cube_products=[],
        moment_products=[],
        peak_products=["dirty"],
        output_prefix=output_prefix,
    )

    names = {path.name for path in output_paths}
    assert len(names) == 9, names
    assert not any("niter" in name for name in names), (
        "the CLEAN iteration map means RM-CLEAN ran for dirty-only peaks"
    )


def test_rmclean_niter_map_is_always_written(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    """Unconditional whenever RM-CLEAN runs, and not an option: it is the map
    that says where CLEAN hit max_iter instead of converging, and that gets
    asked after the run, when producing it on demand means repeating the stage."""
    stokes_q_cube, stokes_u_cube = qu_cubes
    output_prefix = tmp_path / "field"

    output_paths = _synth_and_write(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
        rmclean_options=RMCleanOptions(max_iter=20),
        cube_products=[],
        moment_products=["clean"],
        output_prefix=output_prefix,
    )

    niter_path = Path(f"{output_prefix}.fdf.clean.niter.fits")
    assert niter_path in output_paths
    niter = fits.getdata(niter_path)
    assert niter.shape == (NY, NX)
    # Integer, and capped by max_iter rather than running away
    assert np.issubdtype(niter.dtype, np.integer)
    assert niter.max() <= 20


def test_multiscale_rmclean_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    """flint does not support multiscale RM-CLEAN for now. A strategy asking for
    it has to be told so at load, rather than have the request quietly dropped
    and the run look like it honoured the setting.
    """
    strategy_path = tmp_path / "strategy.yaml"
    strategy_path.write_text(
        "defaults:\n"
        "  rmsynth:\n"
        "    auto_mask: 7\n"
        "version: 0.2\n"
        "rmsynth:\n"
        "  rmclean:\n"
        "    multiscale: true\n"
    )
    from flint.configuration import get_options_from_strategy, load_strategy_yaml

    strategy = load_strategy_yaml(input_yaml=strategy_path, verify=False)
    options = get_options_from_strategy(
        strategy=strategy, operation="rmsynth", mode="rmclean"
    )
    with pytest.raises(ValidationError, match="multiscale"):
        RMCleanOptions(**options)


def test_stokes_i_fit_on_noise_stays_finite(tmp_path: Path) -> None:
    """With the SNR cut working, a noise-only cube must come back with no
    polarised flux and nothing infinite.

    A power law fitted to a noise spectrum is unconstrained and can dip to
    ~1e-10 mid-band; Q/U divided by that is an infinite FDF and an infinite
    mom0. The cut is what stops those pixels being fitted at all.
    """
    q_cube, u_cube, i_cube, i_weight_cube = _make_noise_only_cubes(tmp_path)
    output_prefix = tmp_path / "noise_field"

    _synth_and_write(
        stokes_q_cube=q_cube,
        stokes_u_cube=u_cube,
        stokes_i_cube=i_cube,
        rmsynth_options=RMSynthOptions(stokes_i_snr_cut=5.0),
        rmclean_options=RMCleanOptions(),
        cube_products=[],
        moment_products=["dirty", "clean"],
        output_prefix=output_prefix,
        stokes_i_weight_cube=i_weight_cube,
    )

    for label in ("dirty", "clean"):
        mom0 = fits.getdata(Path(f"{output_prefix}.fdf.{label}.mom0.fits"))
        assert np.isfinite(mom0).all(), f"{label} mom0 has non-finite pixels"
        # Nothing clears the moment threshold, so there is no polarised flux
        assert np.allclose(mom0, 0.0), f"{label} mom0 found flux in pure noise"


@pytest.mark.parametrize(
    ("cube_products", "moment_products"),
    [
        ([], ["clean"]),
        ([], ["clean", "model"]),
        (["clean"], ["clean"]),
        (["clean", "model"], ["clean", "model"]),
        (["dirty", "clean", "model"], ["dirty", "clean", "model"]),
    ],
    ids=["moments", "two-moments", "cube+moments", "two-cubes", "everything"],
)
def test_rmclean_runs_once_per_chunk_whatever_is_requested(
    tmp_path: Path,
    qu_cubes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    cube_products: list[FDFLabel],
    moment_products: list[FDFLabel],
) -> None:
    """RM-CLEAN is the expensive part of the stage, and every requested product
    descends from one ``dask.delayed`` call per spatial chunk, so it must run
    once per chunk no matter how many products are asked for.

    It is easy to lose: ``dask.array.Array.to_zarr`` optimises the graph it
    captures, and the blockwise fuse pass inlines the shared RM-CLEAN task into
    each consumer branch, giving every cube its own private copy. Before this
    was pinned down, two cubes ran RM-CLEAN twice per chunk and three ran it
    three times -- invisible in the output, just twice or three times the
    runtime of the slowest stage.
    """
    import dask
    import rm_lite.tools_3d.rmclean as rmclean_mod

    stokes_q_cube, stokes_u_cube = qu_cubes

    calls: list[int] = []
    original = rmclean_mod._rmclean_on_block

    def counting_clean_block(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(rmclean_mod, "_rmclean_on_block", counting_clean_block)
    # write_rm_products picks the process scheduler when cleaning, which would
    # put the counter in a subprocess. Fusion, not the scheduler, is what
    # duplicates the task, so counting under threads measures the same thing.
    real_compute = dask.compute
    monkeypatch.setattr(
        dask,
        "compute",
        lambda *a, **k: real_compute(*a, **{**k, "scheduler": "threads"}),
    )

    synth_results = _run_rmsynth_3d(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
    )
    n_chunks = (
        synth_results.fdf_dirty_cube.numblocks[1]
        * synth_results.fdf_dirty_cube.numblocks[2]
    )
    clean_results = run_rmclean_3d(
        rm_synth_results=synth_results, rmclean_options=RMCleanOptions()
    )
    write_rm_products(
        synth_results=synth_results,
        clean_results=clean_results,
        stokes_q_cube=stokes_q_cube,
        rmsynth_options=RMSynthOptions(),
        rmclean_options=RMCleanOptions(),
        cube_products=cube_products,
        moment_products=moment_products,
        peak_products=[],
        output_prefix=tmp_path / "test_field",
    )

    assert len(calls) == n_chunks, (
        f"RM-CLEAN ran {len(calls) / n_chunks:.0f}x per chunk for "
        f"cubes={cube_products}, moments={moment_products}"
    )


def _rmclean_call_counter(tally_path: Path):
    """A ``_rmclean_on_block`` wrapper that tallies calls through a file.

    A closure counting into a list cannot work here: `distributed` serialises
    the task graph even when its workers are threads in this process, so the
    worker gets a copy of the list and the appends never come back. A file is
    the counter that survives that round trip.
    """
    import rm_lite.tools_3d.rmclean as rmclean_mod

    original = rmclean_mod._rmclean_on_block

    def counting(*args: object, **kwargs: object) -> object:
        with open(tally_path, "a") as handle:
            handle.write("x")
        return original(*args, **kwargs)

    return counting


def test_rmclean_runs_once_per_chunk_on_a_distributed_client(
    tmp_path: Path, qu_cubes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same once-per-chunk guarantee as the threaded test, on the path a real
    pipeline run takes.

    ``_compute_rm_products`` submits to a distributed Client as futures so it can
    report each product as it lands. Submitting them one at a time instead would
    rebuild the shared synthesis/RM-CLEAN graph per product -- invisible in the
    output, just N times the runtime of the slowest stage. The threaded test
    cannot see this path at all, since it forces the local scheduler.
    """
    import rm_lite.tools_3d.rmclean as rmclean_mod
    from distributed import Client, LocalCluster

    stokes_q_cube, stokes_u_cube = qu_cubes
    tally = tmp_path / "rmclean_calls"
    tally.write_text("")
    monkeypatch.setattr(rmclean_mod, "_rmclean_on_block", _rmclean_call_counter(tally))

    # processes=False keeps the workers in this process, so the monkeypatch is
    # in place for them -- the graph is still serialised, hence the file tally
    cluster = LocalCluster(
        n_workers=2,
        threads_per_worker=1,
        processes=False,
        dashboard_address=None,
        silence_logs=logging.ERROR,
    )
    try:
        with Client(cluster) as client:
            synth_results = _run_rmsynth_3d(
                stokes_q_cube=stokes_q_cube,
                stokes_u_cube=stokes_u_cube,
                rmsynth_options=RMSynthOptions(),
            )
            clean_results = run_rmclean_3d(
                rm_synth_results=synth_results, rmclean_options=RMCleanOptions()
            )
            n_chunks = int(
                np.prod([len(chunk) for chunk in synth_results.fdf_dirty_cube.chunks])
            )
            # Three FDFs' worth of moments off one shared graph
            write_rm_products(
                synth_results=synth_results,
                clean_results=clean_results,
                stokes_q_cube=stokes_q_cube,
                rmsynth_options=RMSynthOptions(),
                rmclean_options=RMCleanOptions(),
                cube_products=[],
                moment_products=["clean", "dirty", "model"],
                peak_products=[],
                output_prefix=tmp_path / "field",
                dask_client=client,
            )
    finally:
        cluster.close()

    assert len(tally.read_text()) == n_chunks, (
        "nine moment maps off three FDFs must still clean each chunk once"
    )


def test_computing_from_inside_a_worker_does_not_deadlock() -> None:
    """The products are computed from a prefect task, which under
    ``DaskTaskRunner`` runs on a worker.

    ``prefect_dask.get_dask_client`` hands back a plain ``Client``, not a
    ``worker_client``, so nothing secedes on our behalf. Waiting on futures from
    a worker thread while the tasks being waited on need worker threads is a
    deadlock, and with one thread per worker it is a certainty rather than a
    race. ``dask.compute(scheduler=client)`` never showed it, because
    ``Client.get`` secedes for you.

    One worker with one thread is the whole reproduction: the submitted call
    holds that thread, so anything it waits on can never be scheduled.
    """
    import dask
    from distributed import Client, LocalCluster, get_client

    from flint.rmsynth import _compute_rm_products

    def compute_on_the_worker() -> dict[str, object]:
        return _compute_rm_products(
            compute_targets={"only": dask.delayed(int)(7)},
            fuse_config={},
            scheduler=get_client(),
            workload="1 product (test)",
        )

    cluster = LocalCluster(
        n_workers=1,
        threads_per_worker=1,
        processes=False,
        dashboard_address=None,
        silence_logs=logging.ERROR,
    )
    try:
        with Client(cluster) as client:
            future = client.submit(compute_on_the_worker)
            # Without seceding this never returns; the timeout is the assertion
            assert future.result(timeout=90) == {"only": 7}
    finally:
        cluster.close()


def test_a_failed_product_is_raised_not_dropped(tmp_path: Path) -> None:
    """Draining futures as they complete must not swallow one that failed.

    ``dask.compute`` raises the worker's exception directly; futures hand it back
    only when asked, so a product that raised could otherwise go missing from the
    results and be noticed as a KeyError somewhere further down instead.
    """
    import dask
    from distributed import Client, LocalCluster

    from flint.rmsynth import _compute_rm_products

    def explode() -> None:
        msg = "this product could not be computed"
        raise ValueError(msg)

    cluster = LocalCluster(
        n_workers=1,
        threads_per_worker=1,
        processes=False,
        dashboard_address=None,
        silence_logs=logging.ERROR,
    )
    try:
        with Client(cluster) as client:
            with pytest.raises(ValueError, match="could not be computed"):
                _compute_rm_products(
                    compute_targets={
                        "fine": dask.delayed(int)(1),
                        "broken": dask.delayed(explode)(),
                    },
                    fuse_config={},
                    scheduler=client,
                    workload="2 products (test)",
                )
    finally:
        cluster.close()


def test_stokes_i_fit_does_not_multiply_with_requested_products(
    tmp_path: Path, qu_cubes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-pixel Stokes I fit is the most expensive step of the stage, so the
    number of FDF products asked for must not scale it -- the same guard as
    ``test_rmclean_runs_once_per_chunk_whatever_is_requested``, for the other
    expensive kernel.

    The count is compared between product mixes rather than against a fixed
    number on purpose. rm-lite's ``run_rmclean`` takes the dirty cube through
    ``to_delayed()``, which optimises and so re-keys the synthesis graph, so the
    copy RM-CLEAN consumes is not the one the Stokes I maps are sliced from and
    the fit currently runs twice per chunk however little is requested. That is
    upstream and constant; what flint controls, and what this pins, is that its
    own batching adds nothing on top.
    """
    import dask
    import rm_lite.utils.fitting as fitting_mod

    stokes_q_cube, stokes_u_cube = qu_cubes
    stokes_i_cube = _make_i_cube(tmp_path)

    calls: list[int] = []
    original = fitting_mod._fit_stokes_i_block

    def counting_fit_block(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(fitting_mod, "_fit_stokes_i_block", counting_fit_block)
    # As above: the counter has to stay in this process, and it is the graph
    # rather than the scheduler that decides how often the fit is called.
    real_compute = dask.compute
    monkeypatch.setattr(
        dask,
        "compute",
        lambda *a, **k: real_compute(*a, **{**k, "scheduler": "threads"}),
    )
    stokes_i_weight_cube = _make_i_weight_cube(tmp_path)

    # Built once, and never computed, only to read the chunking off it
    n_chunks = _run_rmsynth_3d(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
        stokes_i_cube=stokes_i_cube,
        stokes_i_weight_cube=stokes_i_weight_cube,
    ).fdf_dirty_cube.numblocks[1:]
    n_chunks = n_chunks[0] * n_chunks[1]

    counts: dict[str, float] = {}
    product_mixes: tuple[tuple[list[FDFLabel], list[FDFLabel]], ...] = (
        ([], ["clean"]),
        ([], ["dirty", "clean"]),
        (["clean"], ["clean"]),
        (["dirty", "clean"], ["dirty", "clean"]),
    )
    for cube_products, moment_products in product_mixes:
        calls.clear()
        _synth_and_write(
            stokes_q_cube=stokes_q_cube,
            stokes_u_cube=stokes_u_cube,
            stokes_i_cube=stokes_i_cube,
            stokes_i_weight_cube=stokes_i_weight_cube,
            rmsynth_options=RMSynthOptions(),
            rmclean_options=RMCleanOptions(),
            cube_products=cube_products,
            moment_products=moment_products,
            output_prefix=tmp_path / "test_field",
        )
        counts[f"cubes={cube_products}, moments={moment_products}"] = (
            len(calls) / n_chunks
        )

    assert len(set(counts.values())) == 1, (
        f"the Stokes I fit count moves with the requested products: {counts}"
    )


def test_error_cubes_cannot_be_a_weight_and_a_noise_at_once() -> None:
    """A weight cube is 1/sigma**2 and a noise cube is sigma. rm-lite takes
    either through one argument and is told which by a flag, so confusing them
    inverts the noise. One argument of a tagged type makes "both" unsayable and
    "neither, but something cube-shaped" a validation error rather than a silent
    guess at which was meant.
    """
    paths = {"q": Path("q.fits"), "u": Path("u.fits")}

    assert StokesWeightCubes(**paths).kind == "weight"
    assert StokesNoiseCubes(**paths).kind == "noise"

    # The tag is what survives the serialisation prefect does to task inputs.
    # Without it pydantic rebuilds a union by first match, and a noise cube
    # coming back as a weight would invert the noise by 1/sigma**2.
    options = RMSynthFieldOptions(
        stokes_cubes=StokesCubes(**paths), error_cubes=StokesNoiseCubes(**paths)
    )
    rebuilt = RMSynthFieldOptions.model_validate(options.model_dump())
    assert isinstance(rebuilt.error_cubes, StokesNoiseCubes)
    assert isinstance(
        RMSynthFieldOptions.model_validate(
            RMSynthFieldOptions(error_cubes=StokesWeightCubes(**paths)).model_dump()
        ).error_cubes,
        StokesWeightCubes,
    )

    # An untagged trio says nothing about which it is, so it is refused
    with pytest.raises(ValidationError):
        RMSynthFieldOptions(error_cubes=StokesCubes(**paths))


def test_a_weight_cube_is_not_inverted_but_a_noise_cube_is(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    """The one thing the tag decides once it reaches rm-lite."""
    stokes_q_cube, stokes_u_cube = qu_cubes
    sigma = 1e-3
    freq_hz = np.linspace(700e6, 1300e6, N_CHAN)

    captured: dict[str, object] = {}

    def _capture(**kwargs: object) -> None:
        captured.update(kwargs)
        raise RuntimeError("stop here")

    weights = _make_weight_cube(tmp_path, (N_CHAN, NY, NX), freq_hz, sigma, "as_weight")
    for error_cubes, expected in (
        (StokesWeightCubes(q=weights, u=weights), True),
        (StokesNoiseCubes(q=weights, u=weights), False),
    ):
        with patch("flint.rmsynth.rmsynth_3d_from_fits", side_effect=_capture):
            with pytest.raises(RuntimeError, match="stop here"):
                run_rmsynth_3d(
                    stokes_cubes=StokesCubes(q=stokes_q_cube, u=stokes_u_cube),
                    rmsynth_options=RMSynthOptions(),
                    error_cubes=error_cubes,
                )
        assert captured["noise_files_are_weight"] is expected, error_cubes
