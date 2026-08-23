"""RM-synthesis and RM-CLEAN of Stokes Q/U cubes using the external
``rm-lite`` package (an optional dependency, see the ``rmsynth`` extra).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import dask
import numpy as np
import zarr
from astropy.io import fits
from astropy.wcs import WCS
from dask.distributed import Client

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
    calc_faraday_moments,
)

from flint.exceptions import NotSupportedError
from flint.logging import logger
from flint.options import RMCleanOptions, RMSynthOptions

FDFLabel = Literal["dirty", "clean", "model"]
_MOMENT_NAMES = ("mom0", "mom1", "mom2")


def needs_rmclean(
    cube_products: list[FDFLabel], moment_products: list[FDFLabel]
) -> bool:
    """Whether any requested product requires RM-CLEAN to be run.

    Args:
        cube_products (list[FDFLabel]): Requested FDF cubes
        moment_products (list[FDFLabel]): Requested Faraday moment maps

    Returns:
        bool: True if RM-CLEAN is needed
    """
    return any(
        label in ("clean", "model") for label in (*cube_products, *moment_products)
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


def _warn_if_snr_cut_inert(rmsynth_options: RMSynthOptions) -> None:
    """Warn when ``stokes_i_snr_cut`` is set but has no noise to cut on.

    rm-lite scores a pixel with an all-zero Stokes I error as infinite SNR, so
    without a noise estimate the cut passes every pixel and each one gets a full
    ``curve_fit`` -- correct, but ~1000x slower than intended.
    """
    if rmsynth_options.stokes_i_snr_cut is None:
        return
    if rmsynth_options.estimate_stokes_i_noise:
        return
    logger.warning(
        f"stokes_i_snr_cut={rmsynth_options.stokes_i_snr_cut} will do nothing "
        "without estimate_stokes_i_noise: every pixel scores an infinite SNR "
        "and is fitted."
    )


def run_rmsynth_3d(
    stokes_q_cube: Path,
    stokes_u_cube: Path,
    rmsynth_options: RMSynthOptions,
    stokes_i_cube: Path | None = None,
) -> RMSynth3DResults:
    """Run 3D RM-synthesis on Stokes Q/U FITS cubes.

    Args:
        stokes_q_cube (Path): Path to the Stokes Q FITS cube
        stokes_u_cube (Path): Path to the Stokes U FITS cube
        rmsynth_options (RMSynthOptions): Options controlling the synthesis
        stokes_i_cube (Path | None, optional): Path to a Stokes I FITS cube, used to fit a per-pixel fractional-polarisation correction. FDF stays in Q/U flux if not given. Defaults to None.

    Returns:
        RMSynth3DResults: Lazy dirty FDF cube, RMSF cube, and associated parameters
    """
    _check_cubes_memmappable(stokes_q_cube, stokes_u_cube, stokes_i_cube)

    if stokes_i_cube is not None:
        _warn_if_snr_cut_inert(rmsynth_options)

    stokes_i_kwargs = (
        {
            "stokes_i_file": stokes_i_cube,
            "fit_order": rmsynth_options.fit_order,
            "fit_function": rmsynth_options.fit_function,
            "stokes_i_snr_cut": rmsynth_options.stokes_i_snr_cut,
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
        phi_max_radm2=rmsynth_options.phi_max_radm2,
        d_phi_radm2=rmsynth_options.d_phi_radm2,
        n_samples=rmsynth_options.n_samples,
        weight_type=rmsynth_options.weight_type,
        robust=rmsynth_options.robust,
        nufft_nthreads=rmsynth_options.nufft_nthreads,
        target_chunk_mb=rmsynth_options.target_chunk_mb,
        reuse_rmsf=rmsynth_options.reuse_rmsf,
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
        multiscale=rmclean_options.multiscale,
        multiscale_scales=np.asarray(rmclean_options.multiscale_scales, dtype=float)
        if rmclean_options.multiscale_scales
        else None,
        multiscale_n_scales=rmclean_options.multiscale_n_scales,
        multiscale_kernel=rmclean_options.multiscale_kernel,
        multiscale_max_iter_sub_minor=rmclean_options.multiscale_max_iter_sub_minor,
        multiscale_sub_minor_fraction=rmclean_options.multiscale_sub_minor_fraction,
        multiscale_selection=rmclean_options.multiscale_selection,
        multiscale_selection_margin=rmclean_options.multiscale_selection_margin,
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


_STOKES_I_MAP_SUFFIXES = {
    "stokes_i_ref_flux": "stokesi.ref_flux",
    "stokes_i_alpha": "stokesi.alpha",
    "stokes_i_alpha_error": "stokesi.alpha_error",
    "stokes_i_model_order": "stokesi.model_order",
}


def write_stokes_i_fit_maps_to_fits(
    stokes_i_maps: dict[str, np.ndarray],
    reference_header: fits.Header,
    output_prefix: Path,
) -> list[Path]:
    """Write the per-pixel Stokes I fractional-polarisation fit maps: the fitted
    reference flux, spectral index (alpha) and its error, and the fitted
    polynomial order. Cheap 2D maps, always written when a Stokes I cube is used
    and rm-lite actually returned that particular map (e.g. ``alpha_error`` is
    only produced if a Stokes I noise estimate was available).

    Args:
        stokes_i_maps (dict[str, np.ndarray]): Already-computed maps, keyed by
            one of ``_STOKES_I_MAP_SUFFIXES``'s keys.

    Returns:
        list[Path]: The written map paths, one per entry in ``stokes_i_maps``
    """
    header = WCS(reference_header).celestial.to_header()
    output_paths = []
    for key, data in stokes_i_maps.items():
        output_path = Path(f"{output_prefix}.{_STOKES_I_MAP_SUFFIXES[key]}.fits")
        fits.writeto(
            output_path, np.asarray(data, dtype=np.float32), header, overwrite=True
        )
        output_paths.append(output_path)
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
) -> list[Path]:
    """Batch-compute and write the requested RM-synthesis/RM-CLEAN output products.

    Args:
        synth_results (RMSynth3DResults): Results from ``run_rmsynth_3d``
        clean_results (RMClean3DResults | None): Results from ``run_rmclean_3d``, or None if 'clean'/'model' were not requested
        stokes_q_cube (Path): Path to the Stokes Q FITS cube (its header is reused for output WCS)
        rmsynth_options (RMSynthOptions): Options controlling RM-synthesis
        rmclean_options (RMCleanOptions): Options controlling RM-CLEAN, whose ``moment_threshold_snr`` cuts every Faraday moment map
        cube_products (list[FDFLabel]): Which FDF cube(s) to write ('dirty', 'clean', 'model')
        moment_products (list[FDFLabel]): Which FDF(s) to compute Faraday moment maps from
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
    # stokes_i_alpha_error_map is None unless estimate_stokes_i_noise (or a
    # supplied Stokes I error) gives the fit something to propagate; the other
    # maps are None only if the Stokes I fit didn't run at all. Skip whichever
    # are None rather than feeding them to dask.compute/FITS writers.
    stokes_i_maps = {
        "stokes_i_ref_flux": synth_results.stokes_i_ref_flux_map,
        "stokes_i_alpha": synth_results.stokes_i_alpha_map,
        "stokes_i_alpha_error": synth_results.stokes_i_alpha_error_map,
        "stokes_i_model_order": synth_results.stokes_i_model_order_map,
    }
    stokes_i_maps = {k: v for k, v in stokes_i_maps.items() if v is not None}
    compute_targets.update(stokes_i_maps)

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
    compute_keys = list(compute_targets.keys())
    with dask.config.set(fuse_config):
        computed_values = dask.compute(
            *(compute_targets[key] for key in compute_keys), scheduler=scheduler
        )
    computed = dict(
        zip(
            compute_keys,
            computed_values,
        )
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

    if stokes_i_maps:
        output_paths.extend(
            write_stokes_i_fit_maps_to_fits(
                stokes_i_maps={key: computed[key] for key in stokes_i_maps},
                reference_header=reference_header,
                output_prefix=output_prefix,
            )
        )

    return output_paths
