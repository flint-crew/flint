"""Plumbing tests for RM-synthesis via rm-lite"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS
from pydantic import ValidationError

from flint.exceptions import NotSupportedError
from flint.options import RMCleanOptions, RMSynthOptions
from flint.rmsynth import (
    FDFLabel,
    needs_rmclean,
    per_channel_weights_from_linmos,
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
) -> list[Path]:
    if not cube_products and not moment_products:
        return []

    synth_results = run_rmsynth_3d(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=rmsynth_options,
        stokes_i_cube=stokes_i_cube,
        stokes_i_weight_cube=stokes_i_weight_cube,
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
        rmclean_options=rmclean_options,
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
    synth_results = run_rmsynth_3d(
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

    synth_results = run_rmsynth_3d(
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
            run_rmsynth_3d(
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
        run_rmsynth_3d(
            stokes_q_cube=stokes_q_cube,
            stokes_u_cube=stokes_u_cube,
            stokes_i_cube=_make_i_cube(tmp_path),
            rmsynth_options=rmsynth_options,
        )

    assert captured["per_pixel_rmsf"] is True
    assert captured["estimate_stokes_i_noise"] is False


def _make_linmos_weight_cube(
    tmp_path: Path,
    name: str,
    taper: bool = True,
    blank_outside: float | None = None,
    blank_channels: tuple[int, ...] = (),
) -> Path:
    """A weight cube shaped like the one linmos writes: a primary-beam taper,
    zero outside the ``LinmosOptions.cutoff``, and an all-zero plane for any
    channel that was flagged everywhere."""
    freq_hz = np.linspace(700e6, 1300e6, N_CHAN)
    yy, xx = np.mgrid[0:NY, 0:NX]
    radius = np.hypot(yy - (NY - 1) / 2, xx - (NX - 1) / 2)

    plane = np.exp(-(radius**2) / 2.0) if taper else np.ones((NY, NX))
    if blank_outside is not None:
        plane = np.where(radius > blank_outside, 0.0, plane)

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


def test_linmos_weights_collapse_to_one_weight_per_channel(tmp_path: Path) -> None:
    """rm-lite weights channels, not pixels, and derives the cube's single RMSF
    from a ``(n_freq,)`` vector. The linmos cubes are dominated by the
    primary-beam taper and are zero outside the cutoff, so the spatial axes have
    to come off before rm-lite sees them."""
    weights = per_channel_weights_from_linmos(
        stokes_q_weight_cube=_make_linmos_weight_cube(tmp_path, "q", blank_outside=1.5),
        stokes_u_weight_cube=_make_linmos_weight_cube(tmp_path, "u", blank_outside=1.5),
    )

    assert weights.shape == (N_CHAN,)
    assert weights.dtype == np.float64
    # Computed, not lazy: rm-lite's theoretical noise reduces over this, and a
    # lazy one reaches run_rmclean_from_synth as a dask scalar it cannot format
    assert isinstance(weights, np.ndarray)
    assert np.all(np.isfinite(weights))
    # The taper is constant in frequency here, so every channel weighs the same
    assert np.all(weights > 0)
    assert np.allclose(weights, weights[0])


def test_channels_linmos_blanked_get_no_weight(tmp_path: Path) -> None:
    """An all-zero plane is linmos saying the channel was flagged everywhere, so
    it has to reach RM-synthesis with zero weight rather than as a live channel."""
    blanked = (0, 5, N_CHAN - 1)
    weights = per_channel_weights_from_linmos(
        stokes_q_weight_cube=_make_linmos_weight_cube(
            tmp_path, "q", blank_outside=1.5, blank_channels=blanked
        ),
        stokes_u_weight_cube=_make_linmos_weight_cube(
            tmp_path, "u", blank_outside=1.5, blank_channels=blanked
        ),
    )

    assert np.all(weights[list(blanked)] == 0.0)
    live = np.setdiff1d(np.arange(N_CHAN), blanked)
    assert np.all(weights[live] > 0)


def test_unusable_linmos_weights_are_refused(tmp_path: Path) -> None:
    """Both failures leave nothing to weight with, and both are cheap to spot
    here rather than as an empty FDF after the synthesis has run."""
    q_weight = _make_linmos_weight_cube(tmp_path, "q", blank_outside=1.5)

    with pytest.raises(NotSupportedError, match="zero or blank in every channel"):
        per_channel_weights_from_linmos(
            stokes_q_weight_cube=_make_linmos_weight_cube(
                tmp_path, "allzero_q", blank_channels=tuple(range(N_CHAN))
            ),
            stokes_u_weight_cube=_make_linmos_weight_cube(
                tmp_path, "allzero_u", blank_channels=tuple(range(N_CHAN))
            ),
        )

    mismatched = tmp_path / "short.weight.fits"
    fits.writeto(
        mismatched,
        np.ones((N_CHAN - 1, NY, NX), dtype=np.float32),
        fits.getheader(q_weight),
        overwrite=True,
    )
    with pytest.raises(NotSupportedError, match="disagree on shape"):
        per_channel_weights_from_linmos(
            stokes_q_weight_cube=q_weight, stokes_u_weight_cube=mismatched
        )


def test_rmsynth_with_linmos_weights_stays_cleanable(
    tmp_path: Path, qu_cubes: tuple[Path, Path]
) -> None:
    """The whole point of the weight cubes is the run that follows them, and
    RM-CLEAN is where a lazy theoretical noise surfaced: it scales its mask and
    threshold by that scalar and logs it, so a dask one is a TypeError rather
    than a wrong number. The blanked edge is what makes the noise lazy, since it
    is what a real linmos cutoff leaves behind."""
    stokes_q_cube, stokes_u_cube = qu_cubes

    synth_results = run_rmsynth_3d(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=RMSynthOptions(),
        stokes_q_weight_cube=_make_linmos_weight_cube(tmp_path, "q", blank_outside=1.5),
        stokes_u_weight_cube=_make_linmos_weight_cube(tmp_path, "u", blank_outside=1.5),
    )

    fdf_error_noise = synth_results.theoretical_noise.fdf_error_noise
    assert np.isscalar(fdf_error_noise) or np.ndim(fdf_error_noise) == 0
    assert float(fdf_error_noise) > 0
    assert f"{fdf_error_noise:0.3g}"  # what run_rmclean_from_synth does with it

    clean_results = run_rmclean_3d(
        rm_synth_results=synth_results, rmclean_options=RMCleanOptions()
    )
    clean_cube = np.asarray(clean_results.clean_fdf_cube)
    assert np.all(np.isfinite(clean_cube)), (
        "a blanked linmos edge must not put NaNs into pixels that have data"
    )

    phi_arr_radm2 = np.asarray(synth_results.phi_arr_radm2)
    peak_phi = phi_arr_radm2[np.abs(clean_cube).mean(axis=(1, 2)).argmax()]
    assert peak_phi == pytest.approx(PHI_TRUE_RADM2, abs=5.0)


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
    original = rmclean_mod._clean_block

    def counting_clean_block(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(rmclean_mod, "_clean_block", counting_clean_block)
    # write_rm_products picks the process scheduler when cleaning, which would
    # put the counter in a subprocess. Fusion, not the scheduler, is what
    # duplicates the task, so counting under threads measures the same thing.
    real_compute = dask.compute
    monkeypatch.setattr(
        dask,
        "compute",
        lambda *a, **k: real_compute(*a, **{**k, "scheduler": "threads"}),
    )

    synth_results = run_rmsynth_3d(
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
        output_prefix=tmp_path / "test_field",
    )

    assert len(calls) == n_chunks, (
        f"RM-CLEAN ran {len(calls) / n_chunks:.0f}x per chunk for "
        f"cubes={cube_products}, moments={moment_products}"
    )


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
    n_chunks = run_rmsynth_3d(
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
