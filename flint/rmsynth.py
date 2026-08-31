"""RM-synthesis and RM-CLEAN of Stokes Q/U cubes using the external
``rm-lite`` package (an optional dependency, see the ``rmsynth`` extra).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import dask
import numpy as np
import zarr
from astropy.io import fits
from astropy.wcs import WCS
from dask.distributed import Client, as_completed, get_worker, rejoin, secede

# finufft's PyPI wheel vendors its own libomp.dylib (macOS) rather than
# linking the environment's shared one; having both loaded aborts the
# process with "OMP: Error #15: Initializing libomp.dylib, but found
# libomp.dylib already initialized." as soon as anything else (e.g.
# casacore) has already initialised an OpenMP runtime in the process. Must
# be set before finufft/rm_lite are imported.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

from rm_lite.tools_3d.rmclean import (  # noqa: E402
    RMClean3DResults,
    run_rmclean_from_synth,
)
from rm_lite.tools_3d.rmsynth import (  # noqa: E402
    RMSynth3DResults,
    rmsynth_3d_from_fits,
)
from rm_lite.utils.synthesis import (  # noqa: E402
    FaradayMoments,
    FaradayPeaks,
    calc_faraday_moments,
    calc_faraday_peaks,
)

from flint.exceptions import NotSupportedError
from flint.logging import logger
from flint.options import RMCleanOptions, RMSynthOptions

FDFLabel = Literal["dirty", "clean", "model"]
_MOMENT_NAMES = ("mom0", "mom1", "mom2")

# The FDF peak statistics, as {FaradayPeaks field: (file suffix, BUNIT, comment)}.
# Units matter more here than for the moments: three of these are angles in
# degrees and two are Faraday depths, so a map with no BUNIT is easy to misread.
_PEAK_MAPS = {
    "peak_pi": ("peak_pi", "Jy/beam", "peak polarised intensity"),
    "peak_pi_debias": ("peak_pi_debias", "Jy/beam", "debiased peak"),
    "peak_pi_error": ("peak_pi_error", "Jy/beam", "1-sigma on the peak"),
    "peak_rm_radm2": ("peak_rm", "rad/m2", "Faraday depth of the peak"),
    "peak_rm_error_radm2": ("peak_rm_error", "rad/m2", "1-sigma on the depth"),
    "peak_pa_deg": ("peak_pa", "deg", "polarisation angle at the peak"),
    "peak_pa_error_deg": ("peak_pa_error", "deg", "1-sigma on the angle"),
    "peak_pa0_deg": ("peak_pa0", "deg", "intrinsic polarisation angle"),
    "peak_pa0_error_deg": ("peak_pa0_error", "deg", "1-sigma on the intrinsic angle"),
}


def needs_rmclean(
    cube_products: list[FDFLabel],
    moment_products: list[FDFLabel],
    peak_products: list[FDFLabel] | None = None,
) -> bool:
    """Whether any requested product requires RM-CLEAN to be run.

    Args:
        cube_products (list[FDFLabel]): Requested FDF cubes
        moment_products (list[FDFLabel]): Requested Faraday moment maps
        peak_products (list[FDFLabel] | None, optional): Requested FDF peak-statistic maps. Defaults to None.

    Returns:
        bool: True if RM-CLEAN is needed
    """
    return any(
        label in ("clean", "model")
        for label in (*cube_products, *moment_products, *(peak_products or ()))
    )


def _check_cubes_memmappable(*cubes: Path | None) -> None:
    """rm-lite reads each spatial block by reopening the cube with ``memmap=True``.
    astropy cannot memmap a gzip file, so every block read decompresses the whole
    cube into memory. Compress after RM-synthesis, not before."""
    compressed = [cube for cube in cubes if cube is not None and cube.suffix == ".gz"]
    if compressed:
        msg = (
            f"{compressed} are gzip-compressed and cannot be used for RM-synthesis: "
            "astropy cannot memmap a compressed FITS file, so each of the chunked "
            "reads would decompress the entire cube into memory. Run RM-synthesis "
            "on the uncompressed cubes."
        )
        raise NotSupportedError(msg)


def run_rmsynth_3d(
    stokes_q_cube: Path,
    stokes_u_cube: Path,
    rmsynth_options: RMSynthOptions,
    stokes_i_cube: Path | None = None,
    stokes_q_weight_cube: Path | None = None,
    stokes_u_weight_cube: Path | None = None,
    stokes_i_weight_cube: Path | None = None,
) -> RMSynth3DResults:
    """Run 3D RM-synthesis on Stokes Q/U FITS cubes.

    Args:
        stokes_q_cube (Path): Path to the Stokes Q FITS cube
        stokes_u_cube (Path): Path to the Stokes U FITS cube
        rmsynth_options (RMSynthOptions): Options controlling the synthesis
        stokes_i_cube (Path | None, optional): Path to a Stokes I FITS cube, used to fit a per-pixel fractional-polarisation correction. FDF stays in Q/U flux if not given. Defaults to None.
        stokes_q_weight_cube (Path | None, optional): Path to the linmos Stokes Q weight cube, giving each pixel its own channel weights rather than one spectrum estimated from the Q/U cubes. rm-lite requires both Q and U, or neither. Defaults to None.
        stokes_u_weight_cube (Path | None, optional): Path to the linmos Stokes U weight cube. See ``stokes_q_weight_cube``. Defaults to None.
        stokes_i_weight_cube (Path | None, optional): Path to the linmos Stokes I weight cube, used to weight the per-pixel Stokes I fit and to score it against ``stokes_i_snr_cut``. Falls back to ``RMSynthOptions.estimate_stokes_i_noise`` if not given. Defaults to None.

    Returns:
        RMSynth3DResults: Lazy dirty FDF cube, the RMSF, and associated
        parameters. The linmos weights vary with the primary beam, so pixels
        rarely share one RMSF and rm-lite turns on the per-pixel RMSF cube
        itself; see ``RMSynthOptions.per_pixel_rmsf`` for what that costs
    """
    _check_cubes_memmappable(
        stokes_q_cube,
        stokes_u_cube,
        stokes_i_cube,
        stokes_q_weight_cube,
        stokes_u_weight_cube,
        stokes_i_weight_cube,
    )
    stokes_i_kwargs = (
        {
            "stokes_i_file": stokes_i_cube,
            "stokes_i_error_file": stokes_i_weight_cube,
            "fit_order": rmsynth_options.fit_order,
            "fit_function": rmsynth_options.fit_function,
            "stokes_i_snr_cut": rmsynth_options.stokes_i_snr_cut,
            # Only consulted when no Stokes I weight cube is given: rm-lite
            # refuses a stokes_i_snr_cut it has no error to measure against.
            "estimate_stokes_i_noise": rmsynth_options.estimate_stokes_i_noise,
            "compute_model_error": rmsynth_options.compute_model_error,
            "n_error_samples": rmsynth_options.n_error_samples,
        }
        if stokes_i_cube is not None
        else {}
    )
    return rmsynth_3d_from_fits(
        stokes_q_file=stokes_q_cube,
        stokes_u_file=stokes_u_cube,
        stokes_q_error_file=stokes_q_weight_cube,
        stokes_u_error_file=stokes_u_weight_cube,
        # linmos writes 1/sigma**2 directly, so rm-lite must not invert and
        # square these again. Applies to the Stokes I error cube as well
        noise_files_are_weight=True,
        phi_max_radm2=rmsynth_options.phi_max_radm2,
        d_phi_radm2=rmsynth_options.d_phi_radm2,
        n_samples=rmsynth_options.n_samples,
        lam_sq_0_m2=rmsynth_options.lam_sq_0_m2,
        weight_type=rmsynth_options.weight_type,
        robust=rmsynth_options.robust,
        per_pixel_rmsf=rmsynth_options.per_pixel_rmsf,
        nufft_nthreads=rmsynth_options.nufft_nthreads,
        target_chunk_mb=rmsynth_options.target_chunk_mb,
        log_level=logging.INFO,
        **stokes_i_kwargs,
    )


def run_rmclean_3d(
    rm_synth_results: RMSynth3DResults, rmclean_options: RMCleanOptions
) -> RMClean3DResults:
    """Run 3D RM-CLEAN on the results of ``run_rmsynth_3d``.

    Args:
        rm_synth_results (RMSynth3DResults): Results from ``run_rmsynth_3d``
        rmclean_options (RMCleanOptions): Options controlling RM-CLEAN

    Returns:
        RMClean3DResults: Lazy clean/model/residual FDF cubes and moment maps
    """
    return run_rmclean_from_synth(
        rm_synth_3d_results=rm_synth_results,
        auto_mask=rmclean_options.auto_mask,
        auto_threshold=rmclean_options.auto_threshold,
        max_iter=rmclean_options.max_iter,
        gain=rmclean_options.gain,
        moment_threshold_snr=rmclean_options.moment_threshold_snr,
        log_level=logging.INFO,
    )


def write_moment_maps_to_fits(
    moments: FaradayMoments,
    reference_header: fits.Header,
    output_prefix: Path,
    label: FDFLabel,
    debiased_moments: FaradayMoments | None = None,
) -> list[Path]:
    """Write already-computed mom0/mom1/mom2 Faraday moment maps to FITS.

    The moments are built lazily and computed by ``write_rm_products``, which
    keeps the (n_phi, ny, nx) FDF cube they reduce out of this process; only the
    (ny, nx) maps arrive here. See ``_lazy_faraday_moments``.

    Args:
        moments (FaradayMoments): Computed mom0/mom1/mom2 maps, each (ny, nx)
        reference_header (fits.Header): Header to derive the spatial WCS from (e.g. the Stokes Q cube header)
        output_prefix (Path): Common prefix for the output files
        label (FDFLabel): Which FDF the moments came from ('dirty', 'clean', or 'model'), used to name the outputs
        debiased_moments (FaradayMoments | None, optional): Debiased moment set, written alongside with a ``.debiased`` suffix. Defaults to None.

    Returns:
        list[Path]: The written moment-map paths: three (mom0, mom1, mom2), plus
        three more (mom0.debiased, mom1.debiased, mom2.debiased) if debiased_moments is given
    """
    header = WCS(reference_header).celestial.to_header()

    def _write(moment_set: FaradayMoments, suffix: str) -> list[Path]:
        written = []
        for moment_name, moment_map in zip(
            _MOMENT_NAMES,
            (moment_set.mom0, moment_set.mom1, moment_set.mom2),
        ):
            output_path = Path(
                f"{output_prefix}.fdf.{label}.{moment_name}{suffix}.fits"
            )
            fits.writeto(
                output_path,
                np.asarray(moment_map, dtype=np.float32),
                header,
                overwrite=True,
            )
            written.append(output_path)
        return written

    output_paths = _write(moments, suffix="")
    if debiased_moments is not None:
        output_paths.extend(_write(debiased_moments, suffix=".debiased"))

    return output_paths


def _stokes_i_fit_header(
    reference_header: fits.Header,
    ref_freq_hz: float | None,
    fit_function: str | None,
    coeff: tuple[int, str] | None = None,
) -> fits.Header:
    """Build the celestial-WCS header for a Stokes I fit map, stamped with what
    is needed to read it. A spectral index or a reference flux means nothing
    without the frequency it is defined at and the functional form it belongs to,
    so those travel in the header rather than only in flint's file naming.

    Args:
        reference_header (fits.Header): Header to derive the spatial WCS from
        ref_freq_hz (float | None): Frequency the model terms are defined at, omitted from the header if rm-lite did not report one
        fit_function (str | None): The Stokes I fit function the terms belong to ('log' or 'linear'), omitted from the header if not known
        coeff (tuple[int, str] | None, optional): The (popt index, name) of the model term this map holds, for the per-term maps. Defaults to None.

    Returns:
        fits.Header: The header to write the map with
    """
    header = WCS(reference_header).celestial.to_header()
    if ref_freq_hz is not None:
        header["REFFREQ"] = (
            float(ref_freq_hz),
            "Stokes I model reference frequency [Hz]",
        )
    if fit_function is not None:
        header["FITFUNC"] = (fit_function, "Stokes I fit function")
    if coeff is not None:
        index, name = coeff
        header["SICOEFF"] = (name, "Stokes I model term in this map")
        header["SICOEFFI"] = (index, "Index of this term in the fitted popt")
        header.add_comment("0 means the AIC dropped this term: it contributes nothing.")
        header.add_comment("NaN means the pixel was never fitted (below the SNR cut).")
    return header


def write_stokes_i_fit_maps_to_fits(
    stokes_i_maps: dict[str, np.ndarray],
    reference_header: fits.Header,
    output_prefix: Path,
    ref_freq_hz: float | None = None,
    fit_function: str | None = None,
) -> list[Path]:
    """Write the per-pixel Stokes I fractional-polarisation fit maps: the fitted
    reference flux, spectral index (alpha) and its error, and the fitted
    polynomial order. Cheap 2D maps, always written when a Stokes I cube is used
    and rm-lite actually returned that particular map (e.g. ``alpha_error`` is
    only produced if the model error was computed).

    Args:
        stokes_i_maps (dict[str, np.ndarray]): Already-computed maps, keyed by
            one of ``stokes_i_ref_flux``, ``stokes_i_alpha``,
            ``stokes_i_alpha_error`` or ``stokes_i_model_order``.
        reference_header (fits.Header): Header to derive the spatial WCS from
        output_prefix (Path): Common prefix for the output files
        ref_freq_hz (float | None, optional): Frequency the fit is referenced to, recorded in each header. Defaults to None.
        fit_function (str | None, optional): The Stokes I fit function, recorded in each header. Defaults to None.

    Returns:
        list[Path]: The written map paths, one per entry in ``stokes_i_maps``
    """
    suffixes = {
        "stokes_i_ref_flux": "stokesi.ref_flux",
        "stokes_i_alpha": "stokesi.alpha",
        "stokes_i_alpha_error": "stokesi.alpha_error",
        "stokes_i_model_order": "stokesi.model_order",
    }
    header = _stokes_i_fit_header(
        reference_header=reference_header,
        ref_freq_hz=ref_freq_hz,
        fit_function=fit_function,
    )
    output_paths = []
    for key, data in stokes_i_maps.items():
        output_path = Path(f"{output_prefix}.{suffixes[key]}.fits")
        fits.writeto(
            output_path, np.asarray(data, dtype=np.float32), header, overwrite=True
        )
        output_paths.append(output_path)
    return output_paths


def write_stokes_i_coeff_maps_to_fits(
    coeff_cube: np.ndarray,
    coeff_names: tuple[str, ...],
    reference_header: fits.Header,
    output_prefix: Path,
    ref_freq_hz: float | None = None,
    fit_function: str | None = None,
    coeff_error_cube: np.ndarray | None = None,
) -> list[Path]:
    """Write the fitted Stokes I model terms.

    The terms plus the reference frequency and the fit function *are* the whole
    Stokes I model, so anything downstream can evaluate Stokes I at any frequency
    from these maps alone without carrying the model cube -- but only if it knows
    which plane holds which term, which is why each gets its own named file and
    its ``REFFREQ``/``FITFUNC`` header.

    Two values are meaningful rather than missing, and are recorded in the header
    as well: a zero is the actual value of a term the AIC dropped (it contributes
    nothing to the model, and ``stokesi.model_order`` says how many terms were
    really fitted), while a NaN is a pixel that was never fitted at all.

    Args:
        coeff_cube (np.ndarray): Already-computed model terms, shape (n_coeff, ny, nx) in popt order
        coeff_names (tuple[str, ...]): Name of each plane of ``coeff_cube``, e.g. ('flux', 'alpha', 'beta')
        reference_header (fits.Header): Header to derive the spatial WCS from
        output_prefix (Path): Common prefix for the output files
        ref_freq_hz (float | None, optional): Frequency the terms are defined at, recorded in each header. Defaults to None.
        fit_function (str | None, optional): The Stokes I fit function the terms belong to, recorded in each header. Defaults to None.
        coeff_error_cube (np.ndarray | None, optional): 1-sigma marginal error on each term, shaped like ``coeff_cube``, written alongside with an ``_error`` suffix. Defaults to None.

    Returns:
        list[Path]: The written map paths, one per term, plus one more per term if
        ``coeff_error_cube`` is given
    """
    if len(coeff_names) != coeff_cube.shape[0]:
        msg = (
            f"rm-lite named {len(coeff_names)} Stokes I model terms {coeff_names} "
            f"but returned {coeff_cube.shape[0]} planes, so the maps cannot be "
            "named. This is an rm-lite API mismatch, not a configuration error."
        )
        raise ValueError(msg)

    suffix = "stokesi.coeff"
    output_paths = []
    for index, name in enumerate(coeff_names):
        header = _stokes_i_fit_header(
            reference_header=reference_header,
            ref_freq_hz=ref_freq_hz,
            fit_function=fit_function,
            coeff=(index, name),
        )
        output_path = Path(f"{output_prefix}.{suffix}.{name}.fits")
        fits.writeto(
            output_path,
            np.asarray(coeff_cube[index], dtype=np.float32),
            header,
            overwrite=True,
        )
        output_paths.append(output_path)

        if coeff_error_cube is None:
            continue
        # Marginal, i.e. sqrt(diag(pcov)): it ignores the strong correlations
        # between the terms, so it is not the error on the model itself.
        error_header = header.copy()
        error_header.add_comment(
            "1-sigma marginal error; ignores inter-term correlation."
        )
        error_path = Path(f"{output_prefix}.{suffix}.{name}_error.fits")
        fits.writeto(
            error_path,
            np.asarray(coeff_error_cube[index], dtype=np.float32),
            error_header,
            overwrite=True,
        )
        output_paths.append(error_path)

    return output_paths


def _lazy_faraday_moments(
    fdf_cube: dask.array.Array,
    synth_results: RMSynth3DResults,
    threshold: float | None,
    debias: bool = False,
    debias_filter_size: int = 5,
) -> FaradayMoments:
    """Build the lazy mom0/mom1/mom2 maps of an FDF cube.

    ``calc_faraday_moments`` reduces along the (never-chunked) Faraday-depth
    axis, and ``debias_fdf`` handles dask via ``map_overlap``, so the result is
    three lazy (ny, nx) maps that each spatial chunk contributes to
    independently. Computing these instead of the cube itself is what keeps the
    whole FDF out of the calling worker's memory.
    """
    return calc_faraday_moments(
        fdf_cube,
        phi_arr_radm2=synth_results.phi_arr_radm2,
        fwhm_rmsf_radm2=synth_results.fwhm_rmsf_radm2,
        threshold=threshold,
        debias=debias,
        lam_sq_0_m2=synth_results.lam_sq_0_m2 if debias else None,
        debias_filter_size=debias_filter_size,
    )


def _elapsed(start: float) -> str:
    """Wall time since ``start``, as a compact human-readable string."""
    seconds = time.time() - start
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{seconds:02d}s"


def _describe_rm_workload(
    synth_results: RMSynth3DResults, compute_keys: list[str]
) -> str:
    """One line saying what the coming compute is actually going to chew through.

    Everything here is known before any of it runs, and none of it is otherwise
    reported: the FDF's shape, how many spatial chunks it is cut into, and
    whether rm-lite decided every pixel needs its own RMSF (roughly doubling the
    footprint, and not something flint asked for -- see
    ``RMSynthOptions.per_pixel_rmsf``).
    """
    fdf_cube = synth_results.fdf_dirty_cube
    n_phi, n_y, n_x = fdf_cube.shape
    n_chunks = int(np.prod([len(chunk) for chunk in fdf_cube.chunks]))
    per_pixel_rmsf = synth_results.rmsf_cube is not None
    return (
        f"{len(compute_keys)} products over {n_y}x{n_x} pixels and {n_phi} "
        f"Faraday depths, in {n_chunks} chunks; "
        f"per-pixel RMSF {'on' if per_pixel_rmsf else 'off'}"
    )


@contextmanager
def _seceded_if_on_a_worker() -> Iterator[None]:
    """Give this worker's compute slot back for the duration of a blocking wait.

    ``task_write_rm_products`` is a prefect task, so under ``DaskTaskRunner`` it
    runs *on a worker*, and ``prefect_dask.get_dask_client`` hands back a plain
    ``Client`` rather than a ``worker_client`` -- it does not secede. Waiting on
    futures from there holds the worker's thread while the tasks being waited on
    need worker threads to run, and with one thread per worker that deadlocks the
    cluster outright.

    ``dask.compute(scheduler=client)`` never showed this because ``Client.get``
    secedes for you when it is called inside a worker. Draining the futures by
    hand is what makes it ours to do.

    A no-op off a worker, which is the local-scheduler and standalone case.
    """
    try:
        get_worker()
    except ValueError:
        # Not on a worker, so there is no slot to give back
        yield
        return

    secede()
    try:
        yield
    finally:
        rejoin()


def _compute_rm_products(
    compute_targets: dict[str, Any],
    fuse_config: dict[str, Any],
    scheduler: Client | str,
    workload: str,
) -> dict[str, Any]:
    """Compute every product in one shared pass, reporting each as it lands.

    The whole RM-synthesis stage is one ``compute`` over a lazy graph, so
    without this it is a single silent wait -- hours on a mosaic, with no way to
    tell a slow run from a stuck one. Submitting the targets together (rather
    than one compute per product) is what keeps the synthesis and RM-CLEAN graph
    shared, so it still runs once no matter how many products are asked for --
    getting that wrong is invisible in the output and simply multiplies the
    runtime, so ``test_rmclean_runs_once_per_chunk_on_a_distributed_client``
    holds it for this path.

    Progress needs a distributed ``Client`` to report against futures. The local
    schedulers take the plain ``dask.compute`` path and are logged start-to-end
    only -- that is the unit tests and standalone use, not a pipeline run.

    Args:
        compute_targets (dict[str, Any]): Lazy arrays/delayed writes, keyed by product name
        fuse_config (dict[str, Any]): Dask fusion settings the submission must be made under
        scheduler (Client | str): A distributed Client, or a local scheduler name
        workload (str): Description of the work, logged up front. See ``_describe_rm_workload``

    Returns:
        dict[str, Any]: The computed value for each key in ``compute_targets``
    """
    compute_keys = list(compute_targets.keys())
    start = time.time()

    if not isinstance(scheduler, Client):
        logger.info(f"Computing RM products ({workload}), scheduler={scheduler!r}")
        with dask.config.set(fuse_config):
            computed_values = dask.compute(
                *(compute_targets[key] for key in compute_keys), scheduler=scheduler
            )
        logger.info(f"Computed {len(compute_keys)} RM products in {_elapsed(start)}")
        return dict(zip(compute_keys, computed_values))

    logger.info(f"Computing RM products ({workload})")
    with dask.config.set(fuse_config):
        # One submission for the whole batch: `client.compute` on a list keeps
        # the shared graph, where a compute per product would rebuild it each time
        futures = scheduler.compute(
            [compute_targets[key] for key in compute_keys], sync=False
        )
    future_to_key = dict(zip(futures, compute_keys))

    computed: dict[str, Any] = {}
    with _seceded_if_on_a_worker():
        for future in as_completed(futures):
            key = future_to_key[future]
            # `.result()` re-raises whatever the worker raised, so a failed
            # product still surfaces here rather than being dropped
            computed[key] = future.result()
            # Elapsed since submission, not this product's own runtime: they
            # share one graph, so "how far into the run are we" is the
            # answerable question
            logger.info(
                f"[{len(computed):>2}/{len(compute_keys)}] {key} at {_elapsed(start)}"
            )

    logger.info(f"Computed {len(compute_keys)} RM products in {_elapsed(start)}")
    return computed


def write_rmclean_niter_map_to_fits(
    niter_map: np.ndarray, reference_header: fits.Header, output_prefix: Path
) -> Path:
    """Write the per-pixel RM-CLEAN iteration count.

    One small integer map, written whenever RM-CLEAN runs rather than on
    request: it is the map that answers "why does this field look wrong" --
    where CLEAN hit ``max_iter`` instead of converging on the threshold -- and
    that question gets asked after the run, when producing it on demand would
    mean repeating the whole stage.

    Args:
        niter_map (np.ndarray): Computed (ny, nx) iteration count
        reference_header (fits.Header): Header to derive the spatial WCS from
        output_prefix (Path): Common prefix for the output file

    Returns:
        Path: The written map path
    """
    header = WCS(reference_header).celestial.to_header()
    header["BUNIT"] = ("", "CLEAN iterations")
    output_path = Path(f"{output_prefix}.fdf.clean.niter.fits")
    # int32 rather than rm-lite's int64: max_iter is 1e5 by default, so half the
    # bytes carry every value this can hold
    fits.writeto(
        output_path, np.asarray(niter_map, dtype=np.int32), header, overwrite=True
    )
    return output_path


def write_peak_maps_to_fits(
    peaks: FaradayPeaks,
    reference_header: fits.Header,
    output_prefix: Path,
    label: FDFLabel,
) -> list[Path]:
    """Write the already-computed FDF peak-statistic maps to FITS.

    Args:
        peaks (FaradayPeaks): Computed peak maps, each (ny, nx)
        reference_header (fits.Header): Header to derive the spatial WCS from
        output_prefix (Path): Common prefix for the output files
        label (FDFLabel): Which FDF the peaks came from, used to name the outputs

    Returns:
        list[Path]: The written map paths, nine per FDF
    """
    celestial = WCS(reference_header).celestial.to_header()
    output_paths = []
    for field, (suffix, unit, comment) in _PEAK_MAPS.items():
        header = celestial.copy()
        header["BUNIT"] = (unit, comment)
        output_path = Path(f"{output_prefix}.fdf.{label}.{suffix}.fits")
        fits.writeto(
            output_path,
            np.asarray(getattr(peaks, field), dtype=np.float32),
            header,
            overwrite=True,
        )
        output_paths.append(output_path)
    return output_paths


def write_rm_products(
    synth_results: RMSynth3DResults,
    clean_results: RMClean3DResults | None,
    stokes_q_cube: Path,
    rmsynth_options: RMSynthOptions,
    rmclean_options: RMCleanOptions,
    cube_products: list[FDFLabel],
    moment_products: list[FDFLabel],
    output_prefix: Path,
    dask_client: Client | None = None,
    peak_products: list[FDFLabel] | None = None,
) -> list[Path]:
    """Batch-compute and write the requested RM-synthesis/RM-CLEAN output products.

    Args:
        synth_results (RMSynth3DResults): Results from ``run_rmsynth_3d``
        clean_results (RMClean3DResults | None): Results from ``run_rmclean_3d``, or None if 'clean'/'model' were not requested
        stokes_q_cube (Path): Path to the Stokes Q FITS cube (its header is reused for output WCS)
        rmsynth_options (RMSynthOptions): Options controlling RM-synthesis
        rmclean_options (RMCleanOptions): Options controlling RM-CLEAN
        cube_products (list[FDFLabel]): Which FDF cube(s) to write ('dirty', 'clean', 'model')
        moment_products (list[FDFLabel]): Which FDF(s) to compute Faraday moment maps from
        peak_products (list[FDFLabel] | None, optional): Which FDF(s) to write peak-statistic maps from, nine per entry. Defaults to None.
        output_prefix (Path): Common prefix for the output files
        dask_client (Client | None, optional): A distributed Client (e.g. the one backing a Prefect ``DaskTaskRunner``) to compute across, rather than just the local worker. Defaults to None.

    Returns:
        list[Path]: Every FITS path written
    """
    if os.environ.get("OMP_NUM_THREADS") != "1":
        logger.warning(
            "OMP_NUM_THREADS is not set to '1'. rm-lite's dask parallelisation guide "
            "warns this oversubscribes cores when combined with Dask-level parallelism."
        )

    run_clean = clean_results is not None

    fdf_sources = {"dirty": synth_results.fdf_dirty_cube}
    if clean_results is not None:
        fdf_sources["clean"] = clean_results.clean_fdf_cube
        fdf_sources["model"] = clean_results.model_fdf_cube

    # FDF cubes are only ever written to zarr, chunk-by-chunk with each worker
    # writing its own chunk. Gathering an (n_phi, ny, nx) cube into this process
    # to write it as FITS is tens of GB on a real mosaic.
    zarr_store_path = Path(f"{output_prefix}.fdf.zarr") if cube_products else None

    # Blockwise fusion has to be off for both the `dask.array.store` below and
    # the `dask.compute` at the end of this function, or the FDF cubes cost an
    # extra RM-CLEAN pass each -- see the comment on `zarr_arrays`. Only worth it
    # when a cube is actually requested: with fusion off, dask keeps each
    # elementwise step of a moment reduction as its own chunk-sized key instead
    # of folding it into the task that produced it, which raises peak memory on
    # the moment-only path (the default) for no gain there.
    fuse_config = {"optimization.fuse.active": False} if cube_products else {}

    compute_targets: dict[str, Any] = {}
    if cube_products:
        # One `dask.array.store` for every cube, fusion off. `to_zarr` runs its
        # own `store`, which optimises the graph it captures and fuses the shared
        # per-chunk RM-CLEAN task into each cube's private copy -- so asking for
        # two cubes ran RM-CLEAN twice. The copy is made when the `Delayed` is
        # built, so `optimize_graph` at compute time cannot undo it.
        zarr_arrays = [
            zarr.create(
                shape=fdf_sources[label].shape,
                chunks=[chunk[0] for chunk in fdf_sources[label].chunks],
                dtype=fdf_sources[label].dtype,
                store=str(zarr_store_path),
                path=label,
                overwrite=True,
            )
            for label in cube_products
        ]
        with dask.config.set(fuse_config):
            compute_targets["zarr_cubes"] = dask.array.store(
                [fdf_sources[label] for label in cube_products],
                zarr_arrays,
                lock=False,
                compute=False,
            )
        # Without the Faraday depth axis the cubes are not self-describing. Tiny,
        # and with no upstream graph to share, so it is written here and now.
        zarr.create(
            shape=synth_results.phi_arr_radm2.shape,
            chunks=synth_results.phi_arr_radm2.shape,
            dtype=synth_results.phi_arr_radm2.dtype,
            store=str(zarr_store_path),
            path="phi_arr_radm2",
            overwrite=True,
        )[:] = synth_results.phi_arr_radm2

    # Moments enter the batch as their lazy (ny, nx) maps, never as the FDF cube
    # they reduce: gathering the cube here would pull the whole (n_phi, ny, nx)
    # array into this one worker, which for a mosaic-sized cube is tens of GB per
    # requested label.
    #
    # Every FDF gets the same amplitude cut, the dirty one included. mom0 is
    # sum(|FDF|) over the whole Faraday depth axis, so with no cut an off-source
    # pixel integrates hundreds of noise samples into a large positive floor
    # (mom1/mom2 are then weighted by that noise and mean nothing). rm-lite
    # applies this same cut inside RM-CLEAN to its own moment maps, which flint
    # does not use, so it is rederived here from the shared theoretical noise.
    moment_threshold = (
        rmclean_options.moment_threshold_snr
        * synth_results.theoretical_noise.fdf_error_noise
    )
    for label in moment_products:
        moments = _lazy_faraday_moments(
            fdf_cube=fdf_sources[label],
            synth_results=synth_results,
            threshold=moment_threshold,
        )
        for name, moment_map in zip(_MOMENT_NAMES, moments):
            compute_targets[f"moment.{label}.{name}"] = moment_map
        if rmsynth_options.debias_moments:
            debiased = _lazy_faraday_moments(
                fdf_cube=fdf_sources[label],
                synth_results=synth_results,
                threshold=None,
                debias=True,
                debias_filter_size=rmsynth_options.debias_filter_size,
            )
            for name, moment_map in zip(_MOMENT_NAMES, debiased):
                compute_targets[f"debiased.{label}.{name}"] = moment_map
    # Unconditional whenever RM-CLEAN ran: one small integer map, and the
    # diagnostic you want already written rather than a stage to repeat.
    if run_clean:
        assert clean_results is not None  # run_clean is `clean_results is not None`
        compute_targets["rmclean_niter"] = clean_results.iter_count_map

    # rm-lite returns peaks of its own on RMClean3DResults, but only for the
    # clean FDF and only under its own threshold. Deriving them here is what
    # lets any requested FDF have them -- the dirty one included, which needs no
    # RM-CLEAN at all -- under the same cut flint gives its moments.
    for label in peak_products or ():
        peaks = calc_faraday_peaks(
            fdf_sources[label],
            phi_arr_radm2=synth_results.phi_arr_radm2,
            fwhm_rmsf_radm2=synth_results.fwhm_rmsf_radm2,
            fdf_error=synth_results.theoretical_noise.fdf_error_noise,
            lam_sq_0_m2=synth_results.lam_sq_0_m2,
            lambda_sq_arr_m2=synth_results.lambda_sq_arr_m2,
            threshold=moment_threshold,
        )
        for field in _PEAK_MAPS:
            compute_targets[f"peak.{label}.{field}"] = getattr(peaks, field).astype(
                np.float32
            )

    # stokes_i_alpha_error_map is None unless compute_model_error gives the fit
    # something to propagate; the other maps are None only if the Stokes I fit
    # didn't run at all. Skip whichever are None rather than feeding them to
    # dask.compute/FITS writers.
    stokes_i_maps = {
        "stokes_i_ref_flux": synth_results.stokes_i_ref_flux_map,
        "stokes_i_alpha": synth_results.stokes_i_alpha_map,
        "stokes_i_alpha_error": synth_results.stokes_i_alpha_error_map,
        "stokes_i_model_order": synth_results.stokes_i_model_order_map,
    }
    # Cast before the gather, not after. rm-lite builds these in float64 and the
    # FITS writers put them out as float32, so gathering them at full width buys
    # nothing and doubles what this one process has to hold: at 16032^2 each plane
    # is 2.1 GB as float64 against 1.0 GB as float32.
    stokes_i_maps = {
        k: v.astype(np.float32) for k, v in stokes_i_maps.items() if v is not None
    }
    compute_targets.update(stokes_i_maps)

    # The fitted Stokes I model terms, (n_coeff, ny, nx) each -- n_coeff of the
    # maps above rather than anything cube-sized, since n_coeff is 3 or 4. Both
    # are None unless a Stokes I cube was actually *fitted*: a supplied model has
    # no fitted terms to report. They are batched here and split into one named
    # map per term after the compute.
    stokes_i_coeff_cubes = {
        "stokes_i_coeff": synth_results.stokes_i_coeff_cube,
        "stokes_i_coeff_error": synth_results.stokes_i_coeff_error_cube,
    }
    stokes_i_coeff_cubes = {
        k: v.astype(np.float32)
        for k, v in stokes_i_coeff_cubes.items()
        if v is not None
    }
    compute_targets.update(stokes_i_coeff_cubes)

    # Batch every lazy array/delayed write needed by the requested products
    # into a single dask.compute call, including the zarr writes above:
    # computing per-product in a loop would redo the shared synthesis/RM-CLEAN
    # graph once per product instead of once total.
    #
    # Per rm-lite's dask parallelisation guide: the threaded scheduler suits
    # the GIL-releasing NUFFT (dirty-only), but RM-CLEAN/Stokes-I fitting are
    # GIL-bound Python loops that need the process scheduler, unless a
    # distributed Client is given, in which case it takes over entirely.
    scheduler = (
        dask_client
        if dask_client is not None
        else ("processes" if run_clean else "threads")
    )
    computed = _compute_rm_products(
        compute_targets=compute_targets,
        fuse_config=fuse_config,
        scheduler=scheduler,
        workload=_describe_rm_workload(
            synth_results=synth_results, compute_keys=list(compute_targets)
        ),
    )

    reference_header = fits.getheader(stokes_q_cube)

    output_paths: list[Path] = []
    if zarr_store_path is not None:
        output_paths.append(zarr_store_path)
    for label in moment_products:
        output_paths.extend(
            write_moment_maps_to_fits(
                moments=FaradayMoments(
                    *(computed[f"moment.{label}.{name}"] for name in _MOMENT_NAMES)
                ),
                reference_header=reference_header,
                output_prefix=output_prefix,
                label=label,
                debiased_moments=FaradayMoments(
                    *(computed[f"debiased.{label}.{name}"] for name in _MOMENT_NAMES)
                )
                if rmsynth_options.debias_moments
                else None,
            )
        )

    if "rmclean_niter" in computed:
        output_paths.append(
            write_rmclean_niter_map_to_fits(
                niter_map=computed["rmclean_niter"],
                reference_header=reference_header,
                output_prefix=output_prefix,
            )
        )

    for label in peak_products or ():
        output_paths.extend(
            write_peak_maps_to_fits(
                peaks=FaradayPeaks(
                    **{field: computed[f"peak.{label}.{field}"] for field in _PEAK_MAPS}
                ),
                reference_header=reference_header,
                output_prefix=output_prefix,
                label=label,
            )
        )

    if stokes_i_maps:
        output_paths.extend(
            write_stokes_i_fit_maps_to_fits(
                stokes_i_maps={key: computed[key] for key in stokes_i_maps},
                reference_header=reference_header,
                output_prefix=output_prefix,
                ref_freq_hz=synth_results.stokes_i_ref_freq_hz,
                fit_function=rmsynth_options.fit_function,
            )
        )

    if "stokes_i_coeff" in stokes_i_coeff_cubes:
        # The plane names are what make the terms usable, so without them the
        # maps are not worth writing -- but they arrive with the cube, so this
        # would take an rm-lite change to reach.
        if synth_results.stokes_i_coeff_names is None:
            logger.warning(
                "rm-lite returned Stokes I model terms with no names for them, so "
                "the per-term maps cannot be written."
            )
        else:
            output_paths.extend(
                write_stokes_i_coeff_maps_to_fits(
                    coeff_cube=computed["stokes_i_coeff"],
                    coeff_names=synth_results.stokes_i_coeff_names,
                    reference_header=reference_header,
                    output_prefix=output_prefix,
                    ref_freq_hz=synth_results.stokes_i_ref_freq_hz,
                    fit_function=rmsynth_options.fit_function,
                    coeff_error_cube=computed.get("stokes_i_coeff_error"),
                )
            )

    return output_paths
