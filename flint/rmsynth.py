"""RM-synthesis and RM-CLEAN of Stokes Q/U cubes using the external
``rm-lite`` package (an optional dependency, see the ``rmsynth`` extra).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import dask
import numpy as np
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
from flint.naming import create_image_cube_name
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
    )


def _phi_header(
    reference_header: fits.Header, phi_arr_radm2: np.ndarray
) -> fits.Header:
    """Build a FDF cube header: the reference header's spatial WCS with the
    spectral axis replaced by a linear Faraday-depth axis."""
    header = WCS(reference_header).celestial.to_header()
    header["NAXIS"] = 3
    header["CTYPE3"] = "FDEPTH"
    header["CUNIT3"] = "rad/m2"
    header["CRPIX3"] = 1
    header["CRVAL3"] = float(phi_arr_radm2[0])
    header["CDELT3"] = float(phi_arr_radm2[1] - phi_arr_radm2[0])
    return header


def write_fdf_cube_to_fits(
    fdf_cube: np.ndarray,
    phi_arr_radm2: np.ndarray,
    reference_header: fits.Header,
    output_path: Path,
) -> Path:
    """Write a (already computed) FDF cube to FITS as amplitude, matching the
    RM-Tools ``_FDFdirty.fits``/``_FDFclean.fits`` convention. Phase is dropped.

    Args:
        fdf_cube (np.ndarray): Complex FDF cube, shape (n_phi, ny, nx)
        phi_arr_radm2 (np.ndarray): Faraday depth values, rad/m^2
        reference_header (fits.Header): Header to derive the spatial WCS from (e.g. the Stokes Q cube header)
        output_path (Path): Output FITS path

    Returns:
        Path: ``output_path``
    """
    header = _phi_header(reference_header=reference_header, phi_arr_radm2=phi_arr_radm2)
    fits.writeto(
        output_path, np.abs(fdf_cube).astype(np.float32), header, overwrite=True
    )
    return output_path


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
        rmclean_options (RMCleanOptions): Options controlling RM-CLEAN
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

    # Cubes written to zarr are stored chunk-by-chunk (one worker writing its
    # own chunk directly) and so must NOT also be gathered to numpy here!
    # only labels also needed for moments (small, cheap to gather) go through
    # the numpy path below.
    write_cubes_as_zarr = rmsynth_options.write_fdfs_to_zarr and bool(cube_products)
    zarr_store_path = Path(f"{output_prefix}.fdf.zarr") if write_cubes_as_zarr else None
    numpy_cube_labels = set() if write_cubes_as_zarr else set(cube_products)

    compute_targets: dict[str, dask.array.Array] = {
        label: fdf_sources[label] for label in numpy_cube_labels
    }
    if write_cubes_as_zarr:
        for label in cube_products:
            compute_targets[f"zarr_{label}"] = fdf_sources[label].to_zarr(
                str(zarr_store_path), component=label, overwrite=True, compute=False
            )

    # Moments enter the batch as their lazy (ny, nx) maps, never as the FDF cube
    # they reduce: gathering the cube here would pull the whole (n_phi, ny, nx)
    # array into this one worker, which for a mosaic-sized cube is tens of GB per
    # requested label. RM-CLEAN already applies this same threshold to its own
    # (unused) moment maps, so it is derived once here from the shared noise.
    clean_moment_threshold = (
        rmclean_options.moment_threshold_snr
        * synth_results.theoretical_noise.fdf_error_noise
    )
    moment_thresholds: dict[FDFLabel, float | None] = {
        "dirty": None,
        "clean": clean_moment_threshold,
        "model": clean_moment_threshold,
    }
    for label in moment_products:
        moments = _lazy_faraday_moments(
            fdf_cube=fdf_sources[label],
            synth_results=synth_results,
            threshold=moment_thresholds[label],
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
    computed = dict(
        zip(
            compute_keys,
            dask.compute(
                *(compute_targets[key] for key in compute_keys), scheduler=scheduler
            ),
        )
    )

    reference_header = fits.getheader(stokes_q_cube)

    output_paths: list[Path] = []
    if write_cubes_as_zarr:
        assert zarr_store_path is not None
        output_paths.append(zarr_store_path)
    else:
        for label in cube_products:
            output_path = create_image_cube_name(
                image_prefix=output_prefix, mode="fdf", suffix=label
            )
            output_paths.append(
                write_fdf_cube_to_fits(
                    fdf_cube=computed[label],
                    phi_arr_radm2=synth_results.phi_arr_radm2,
                    reference_header=reference_header,
                    output_path=output_path,
                )
            )
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
