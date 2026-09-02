"""Flow-level tests for the RM-synthesis pipeline. The unit tests in
``tests/test_rmsynth.py`` mirror this flow's call sequence by hand; these tests
run the real flow so that mirror cannot silently drift, and so the prefect and
dask-distributed wiring (task submission, pickling lazy dask arrays between
tasks, the nested ``get_dask_client()``) is actually exercised."""

from __future__ import annotations

from pathlib import Path

import astropy.units as u
import numpy as np
import pytest
from astropy.io import fits
from distributed import LocalCluster
from prefect import flow
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness
from prefect_dask import DaskTaskRunner
from radio_beam import Beams

from flint.convol import (
    common_beam_from_cubes,
    cubes_share_common_beam,
    usable_beam_mask,
)
from flint.options import FFTBANEOptions, RMSynthFieldOptions
from flint.prefect.common.rmsynth import CommonResolutionCubes
from flint.prefect.flows.rmsynth_pipeline import (
    _resolve_common_resolution_cubes,
    process_rmsynth,
)

from .test_convol import _write_cube_with_beam
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
            rmsynth_result = process_rmsynth.with_options(
                task_runner=DaskTaskRunner(address=cluster.scheduler_address)
            )(rmsynth_field_options=rmsynth_field_options)
        finally:
            cluster.close()

    output_paths = rmsynth_result.written_paths
    assert rmsynth_result.convolved_cubes == [], (
        "the cubes carry one beam already, so nothing should have been convolved"
    )

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
        rmsynth_result = process_rmsynth(
            rmsynth_field_options=RMSynthFieldOptions(
                stokes_q_cube=stokes_q_cube,
                stokes_u_cube=stokes_u_cube,
                cube_products=[],
                moment_products=[],
            )
        )

    assert rmsynth_result.written_paths == []
    assert rmsynth_result.convolved_cubes == []
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
            )(rmsynth_field_options=rmsynth_field_options).written_paths
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


def _resolve_cubes(
    cubes: dict[str, Path],
    output_path: Path,
    beam_cutoff: float | None = None,
    fft_bane_options: FFTBANEOptions | None = None,
) -> CommonResolutionCubes:
    @flow
    def _resolve() -> CommonResolutionCubes:
        return _resolve_common_resolution_cubes(
            stokes_cubes=cubes,
            output_path=output_path,
            beam_cutoff=beam_cutoff,
            fft_bane_options=fft_bane_options,
        )

    with prefect_test_harness(), disable_run_logger():
        return _resolve()


def _resolved_cubes(
    cubes: dict[str, Path], output_path: Path, beam_cutoff: float | None = None
) -> tuple[dict[str, Path], list[Path]]:
    """The cubes to use, and the new ones written, as the callers of
    ``_resolve_common_resolution_cubes`` see them"""
    resolved = _resolve_cubes(
        cubes=cubes, output_path=output_path, beam_cutoff=beam_cutoff
    )
    return resolved.cubes, (list(resolved.cubes.values()) if resolved.convolved else [])


def _cubes_with_beams(tmp_path: Path, offsets: dict[str, float]) -> dict[str, Path]:
    """Flint-named cubes, which is what splitting a cube into named planes needs"""
    return {
        pol: _write_cube_with_beam(
            tmp_path / f"{STEM}.{pol}.linmos.fits", [10.0 + offset] * 3
        )
        for pol, offset in offsets.items()
    }


def test_resolve_common_resolution_cubes_convolves(tmp_path: Path) -> None:
    """Stokes cubes at differing resolutions are replaced by convolved copies
    written into the output directory, leaving the inputs in place"""
    cubes = _cubes_with_beams(tmp_path, {"q": 0.0, "u": 0.5, "i": 1.0})
    output_path = tmp_path / "rmsynth"

    resolved, convolved_cubes = _resolved_cubes(cubes=cubes, output_path=output_path)

    resolved_cubes = list(resolved.values())
    assert all(cube.exists() for cube in cubes.values()), "an input cube was consumed"
    assert not set(resolved_cubes) & set(cubes.values())
    assert all(cube.parent == output_path for cube in resolved_cubes)
    assert cubes_share_common_beam(cube_paths=resolved_cubes)
    # Each Stokes must keep its own data, not be paired to another cube's output
    for stokes, resolved_cube in resolved.items():
        assert resolved_cube.name.startswith(cubes[stokes].stem)
    # The weight cubes are untouched, so the convolved cubes must stay on the
    # input pixel and channel grid
    assert fits.getdata(resolved["q"]).shape == fits.getdata(cubes["q"]).shape
    assert convolved_cubes == resolved_cubes
    # The planes each cube was split into and convolved through are cleaned up
    assert not list(output_path.glob("*.planes"))


def test_resolve_common_resolution_cubes_reuses_matching_cubes(tmp_path: Path) -> None:
    """Cubes already at one resolution are used as they are"""
    cubes = _cubes_with_beams(tmp_path, {"q": 0.0, "u": 0.0, "i": 0.0})
    output_path = tmp_path / "rmsynth"

    resolved, convolved_cubes = _resolved_cubes(cubes=cubes, output_path=output_path)

    assert resolved == cubes
    assert not output_path.exists()
    # Empty, not the inputs echoed back: the racs-all flow concatenates these
    # with the input cubes to spice, and a repeat would be spiced twice
    assert convolved_cubes == []


def test_resolve_common_resolution_cubes_from_natural_resolution(
    tmp_path: Path,
) -> None:
    """The handoff from the polarisation stage: cubes at a 'natural' resolution,
    one beam per channel, are brought to a single 'total' beam covering the whole
    band, and the natural cubes they came from are left alone to be archived"""
    cubes = {
        pol: _write_cube_with_beam(
            tmp_path / f"{STEM}.{pol}.linmos.fits", [10.0, 15.0, 20.0]
        )
        for pol in ("q", "u")
    }
    assert not cubes_share_common_beam(cube_paths=list(cubes.values())), (
        "the inputs must vary with channel for this to be the case under test"
    )

    resolved, convolved_cubes = _resolved_cubes(
        cubes=cubes, output_path=tmp_path / "rmsynth"
    )

    assert convolved_cubes == list(resolved.values())
    assert cubes_share_common_beam(cube_paths=list(resolved.values()))
    # The natural-resolution cubes survive, still varying with channel
    assert all(cube.exists() for cube in cubes.values())
    assert not cubes_share_common_beam(cube_paths=list(cubes.values()))
    # And the one beam has to cover the coarsest channel of the band
    total_beam = common_beam_from_cubes(cube_paths=list(resolved.values()))
    assert total_beam is not None
    assert total_beam.major.to(u.arcsec).value >= 20.0


def test_resolve_common_resolution_cubes_beam_cutoff_blanks_coarse_channels(
    tmp_path: Path,
) -> None:
    """A channel coarser than the cutoff is blanked rather than dragging the
    common beam of every channel out to its resolution"""
    cubes = _cubes_with_beams(tmp_path, {"q": 0.0, "u": 0.5})
    coarse = _write_cube_with_beam(
        tmp_path / f"{STEM}.i.linmos.fits", [10.0, 10.0, 40.0]
    )
    cubes["i"] = coarse

    resolved, _ = _resolved_cubes(
        cubes=cubes, output_path=tmp_path / "rmsynth", beam_cutoff=20.0
    )

    common_beam = common_beam_from_cubes(cube_paths=[resolved["q"]])
    assert common_beam is not None
    assert common_beam.major.to(u.arcsec).value < 20.0, (
        "the 40 arcsec channel was convolved to rather than cut"
    )
    assert np.all(np.isnan(fits.getdata(resolved["i"])[2])), (
        "the channel above the cutoff should be blanked"
    )
    assert np.any(np.isfinite(fits.getdata(resolved["i"])[0]))
    # The blanked channel must not claim a PSF it does not have
    blanked_beam = Beams.from_fits_bintable(fits.open(resolved["i"])["BEAMS"])
    assert usable_beam_mask(beams=blanked_beam)[2] == False  # noqa: E712
    assert usable_beam_mask(beams=blanked_beam)[0]


def test_resolve_common_resolution_cubes_without_usable_beams(tmp_path: Path) -> None:
    """Cubes carrying nothing but the placeholder beams of blanked channels have
    no resolution to make common, and are used as they are rather than handed to
    a common-beam solver that cannot solve them"""
    tiny = float(np.finfo(np.float32).tiny)
    cubes = {
        pol: _write_cube_with_beam(
            tmp_path / f"{STEM}.{pol}.linmos.fits", [0.0, tiny, 0.0]
        )
        for pol in ("q", "u")
    }

    resolved, convolved_cubes = _resolved_cubes(
        cubes=cubes, output_path=tmp_path / "rmsynth"
    )

    assert resolved == cubes
    assert convolved_cubes == []


def test_bane_cubes_are_measured_on_the_convolved_planes(tmp_path: Path) -> None:
    """The noise has to describe the cubes the FDF is built from. Convolving to a
    common beam changes them, so measuring on the polarisation stage's natural
    resolution cubes would describe a resolution rm-synthesis never reads. These
    are measured after the convolution instead, and land on the convolved grid.
    """
    cubes = _cubes_with_beams(tmp_path, {"q": 0.0, "u": 0.5})
    output_path = tmp_path / "rmsynth"

    resolved = _resolve_cubes(
        cubes=cubes,
        output_path=output_path,
        fft_bane_options=FFTBANEOptions(step_size=2, box_size=2),
    )

    assert resolved.convolved
    assert set(resolved.bkg_cubes) == set(resolved.rms_cubes) == {"q", "u"}
    for stokes, rms_cube in resolved.rms_cubes.items():
        assert rms_cube.exists()
        # Same grid as the convolved cube it was measured on, so rm-lite can read
        # the two together
        assert (
            fits.getdata(rms_cube).shape == fits.getdata(resolved.cubes[stokes]).shape
        )
    assert all(cube.exists() for cube in resolved.bkg_cubes.values())
    # Everything written is a frequency cube the racs-all flow has to spice
    assert set(resolved.all_cubes) == {
        *resolved.cubes.values(),
        *resolved.bkg_cubes.values(),
        *resolved.rms_cubes.values(),
    }


def test_no_bane_cubes_without_a_convolution(tmp_path: Path) -> None:
    """Cubes already at one resolution are used as they are, so this stage writes
    nothing and has nothing to measure. The caller's own noise cubes, measured on
    those same cubes by the polarisation stage, are already right."""
    cubes = _cubes_with_beams(tmp_path, {"q": 0.0, "u": 0.0})

    resolved = _resolve_cubes(
        cubes=cubes,
        output_path=tmp_path / "rmsynth",
        fft_bane_options=FFTBANEOptions(step_size=2, box_size=2),
    )

    assert not resolved.convolved
    assert resolved.cubes == cubes
    assert resolved.rms_cubes == {}
    assert resolved.all_cubes == []
