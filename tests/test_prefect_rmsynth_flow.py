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

from .test_rmsynth import NX, NY, PHI_TRUE_RADM2, _make_qu_cubes

# create_name_from_common_fields rejects names it cannot decompose
STEM = "SB12345.BENCH_0000+00.ch0000-0019"


def _renamed_qu_cubes(tmp_path: Path) -> tuple[Path, Path]:
    q_path, u_path = _make_qu_cubes(tmp_path)
    return (
        q_path.rename(tmp_path / f"{STEM}.q.linmos.fits"),
        u_path.rename(tmp_path / f"{STEM}.u.linmos.fits"),
    )


@pytest.mark.slow
def test_process_rmsynth_on_dask_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Most of the runtime here is cluster start up and the worker imports of
    flint/rm_lite/finufft, so shrinking the cubes further does not help."""
    stokes_q_cube, stokes_u_cube = _renamed_qu_cubes(tmp_path)
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
    assert set(output_paths) == {zarr_store} | {
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
