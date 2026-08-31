"""Flow-level tests for the RM-synthesis pipeline. The unit tests in
``tests/test_rmsynth.py`` mirror this flow's call sequence by hand; these tests
run the real flow so that mirror cannot silently drift, and so the prefect and
dask-distributed wiring (task submission, pickling lazy dask arrays between
tasks, the nested ``get_dask_client()``) is actually exercised."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from distributed import LocalCluster
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness
from prefect_dask import DaskTaskRunner

from flint.options import RMSynthFieldOptions
from flint.prefect.flows.rmsynth_pipeline import process_rmsynth

from .test_rmsynth import NX, NY, PHI_TRUE_RADM2, _make_i_cube, _make_qu_cubes

# create_name_from_common_fields rejects names it cannot decompose
STEM = "SB12345.BENCH_0000+00.ch0000-0019"


def _add_degenerate_axis(path: Path) -> Path:
    """Rewrite a (nfreq, ny, nx) cube in the ASKAP/CASA (nfreq, 1, ny, nx) layout,
    moving the spectral axis to FITS axis 4 and inserting a degenerate Stokes axis."""
    data, header = fits.getdata(path, header=True)
    for key in ("CTYPE", "CRVAL", "CRPIX", "CDELT", "CUNIT"):
        if f"{key}3" in header:
            header[f"{key}4"] = header.pop(f"{key}3")
    header["CTYPE3"] = "STOKES"
    header["CRVAL3"] = 1.0
    header["CRPIX3"] = 1.0
    header["CDELT3"] = 1.0
    fits.writeto(path, data[:, np.newaxis], header, overwrite=True)
    return path


def _renamed_qu_cubes(
    tmp_path: Path, degenerate_axis: bool = False
) -> tuple[Path, Path]:
    q_path, u_path = _make_qu_cubes(tmp_path)
    paths = (
        q_path.rename(tmp_path / f"{STEM}.q.linmos.fits"),
        u_path.rename(tmp_path / f"{STEM}.u.linmos.fits"),
    )
    if not degenerate_axis:
        return paths
    # Explicit pair rather than `tuple(map(...))`, which types as tuple[Path, ...]
    return _add_degenerate_axis(paths[0]), _add_degenerate_axis(paths[1])


@pytest.mark.parametrize("degenerate_axis", [False, True], ids=["3d", "askap-4d"])
def test_process_rmsynth_on_dask_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, degenerate_axis: bool
) -> None:
    """Most of the runtime here is cluster start up and the worker imports of
    flint/rm_lite/finufft, so shrinking the cubes further does not help. The
    askap-4d case guards against rm-lite mishandling the degenerate Stokes axis
    that real ASKAP/CASA cubes carry, which 2026.8.0 regressed on."""
    stokes_q_cube, stokes_u_cube = _renamed_qu_cubes(
        tmp_path, degenerate_axis=degenerate_axis
    )
    monkeypatch.setenv("OMP_NUM_THREADS", "1")

    rmsynth_field_options = RMSynthFieldOptions(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        cube_products=["dirty"],
        moment_products=["clean"],
    )

    # Checked: worker processes started inside the harness report the harness'
    # temporary API and database, not the developer's ~/.prefect
    with prefect_test_harness(), disable_run_logger():
        # processes=True so that the pickling of lazy dask arrays between
        # tasks is exercised, which a threaded cluster would not do
        cluster = LocalCluster(
            n_workers=2,
            threads_per_worker=1,
            processes=True,
            memory_limit="2GB",
            dashboard_address=None,
            silence_logs=40,
        )
        try:
            output_paths = process_rmsynth.with_options(
                task_runner=DaskTaskRunner(address=cluster.scheduler_address)
            )(rmsynth_field_options=rmsynth_field_options)
        finally:
            cluster.close()

    zarr_store = tmp_path / f"{STEM}.fdf.zarr"
    # The CLEAN iteration count comes out of every RM-CLEAN run, alongside the
    # zarr store and the requested moments
    assert set(output_paths) == {
        zarr_store,
        tmp_path / f"{STEM}.fdf.clean.niter.fits",
    } | {
        tmp_path / f"{STEM}.fdf.clean.{moment}.fits"
        for moment in ("mom0", "mom1", "mom2")
    }
    for path in output_paths:
        assert path.exists()
    assert not list(tmp_path.glob("*.fdf.*.cube.fits")), (
        "FDF cubes are zarr-only; a FITS cube means the gather path came back"
    )

    clean_mom1 = fits.getdata(tmp_path / f"{STEM}.fdf.clean.mom1.fits")
    assert np.allclose(clean_mom1, PHI_TRUE_RADM2, atol=5.0)

    import zarr

    group = zarr.open(str(zarr_store), mode="r")
    assert set(group.keys()) == {"dirty", "phi_arr_radm2"}
    dirty = group["dirty"][:]
    phi_arr_radm2 = group["phi_arr_radm2"][:]
    assert dirty.shape == (phi_arr_radm2.shape[0], NY, NX)
    peak_phi = phi_arr_radm2[np.argmax(np.abs(dirty).mean(axis=(1, 2)))]
    assert peak_phi == pytest.approx(PHI_TRUE_RADM2, abs=5.0)


def test_process_rmsynth_no_products_submits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stokes_q_cube, stokes_u_cube = _renamed_qu_cubes(tmp_path)
    monkeypatch.setattr(
        "flint.prefect.flows.rmsynth_pipeline.task_rmsynth.submit",
        lambda **kwargs: pytest.fail("task_rmsynth should not be submitted"),
    )

    with prefect_test_harness(), disable_run_logger():
        output_paths = process_rmsynth(
            rmsynth_field_options=RMSynthFieldOptions(
                stokes_q_cube=stokes_q_cube,
                stokes_u_cube=stokes_u_cube,
                cube_products=[],
                moment_products=[],
            )
        )

    assert output_paths == []
    assert not list(tmp_path.glob("*.fdf.*"))


def test_process_rmsynth_with_stokes_i_on_dask_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fractional-polarisation path across a real cluster. The Stokes I fit
    products are the only ones built by a ``map_blocks`` whose output flint
    slices apart after the gather, so they are the ones a pickling or chunking
    problem would strand as None or as the wrong plane -- and an unfitted run
    reports None for all of them, so the failure would only show on the path
    that fits.
    """
    stokes_q_cube, stokes_u_cube = _renamed_qu_cubes(tmp_path)
    stokes_i_cube = _make_i_cube(tmp_path)
    monkeypatch.setenv("OMP_NUM_THREADS", "1")

    rmsynth_field_options = RMSynthFieldOptions(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        stokes_i_cube=stokes_i_cube,
        cube_products=[],
        moment_products=["clean"],
    )

    with prefect_test_harness(), disable_run_logger():
        cluster = LocalCluster(
            n_workers=2,
            threads_per_worker=1,
            processes=True,
            memory_limit="2GB",
            dashboard_address=None,
            silence_logs=40,
        )
        try:
            output_paths = process_rmsynth.with_options(
                task_runner=DaskTaskRunner(address=cluster.scheduler_address)
            )(rmsynth_field_options=rmsynth_field_options)
        finally:
            cluster.close()

    # `_make_i_cube` is a pure power law, so the log fit's terms are flux, alpha
    # and beta, and every pixel is bright enough to clear the SNR cut
    term_paths = {
        term: tmp_path / f"{STEM}.stokesi.coeff.{term}.fits"
        for term in ("flux", "alpha", "beta")
    }
    assert set(output_paths) >= set(term_paths.values()), (
        "the fitted Stokes I model terms came back None or unnamed on the cluster"
    )
    for term, path in term_paths.items():
        data, header = fits.getdata(path, header=True)
        assert data.shape == (NY, NX)
        assert np.all(np.isfinite(data)), f"{term} was not fitted"
        assert header["SICOEFF"] == term
        assert header["FITFUNC"] == "log"
        assert header["REFFREQ"] > 0

    alpha = fits.getdata(term_paths["alpha"])
    assert np.allclose(alpha, -0.7, atol=0.1), (
        "the fitted spectral index does not match the Stokes I cube's -0.7"
    )
