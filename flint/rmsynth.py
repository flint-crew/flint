"""RM-synthesis and RM-CLEAN of Stokes Q/U cubes using the external
``rm-lite`` package (an optional dependency, see the ``rmsynth`` extra).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import dask
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from rm_lite.tools_3d.rmclean import RMClean3DResults, run_rmclean_from_synth
from rm_lite.tools_3d.rmsynth import RMSynth3DResults, rmsynth_3d_from_fits
from rm_lite.utils.synthesis import calc_faraday_moments

from flint.logging import logger
from flint.naming import create_image_cube_name
from flint.options import RMCleanOptions, RMSynthOptions

FDFLabel = Literal["dirty", "clean", "model"]


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
    stokes_i_kwargs = (
        {
            "stokes_i_file": stokes_i_cube,
            "fit_order": rmsynth_options.fit_order,
            "fit_function": rmsynth_options.fit_function,
            "stokes_i_snr_cut": rmsynth_options.stokes_i_snr_cut,
            "estimate_stokes_i_noise": rmsynth_options.estimate_stokes_i_noise,
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


def _phi_header(reference_header: fits.Header, phi_arr_radm2: np.ndarray) -> fits.Header:
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
    fits.writeto(output_path, np.abs(fdf_cube).astype(np.float32), header, overwrite=True)
    return output_path


def write_moment_maps_to_fits(
    fdf_cube: np.ndarray,
    phi_arr_radm2: np.ndarray,
    fwhm_rmsf_radm2: float,
    reference_header: fits.Header,
    output_prefix: Path,
    label: FDFLabel,
    threshold: float | None = None,
) -> list[Path]:
    """Compute and write the mom0/mom1/mom2 Faraday moment maps of an FDF cube.

    Args:
        fdf_cube (np.ndarray): Complex FDF cube, shape (n_phi, ny, nx)
        phi_arr_radm2 (np.ndarray): Faraday depth values, rad/m^2
        fwhm_rmsf_radm2 (float): RMSF FWHM, rad/m^2
        reference_header (fits.Header): Header to derive the spatial WCS from (e.g. the Stokes Q cube header)
        output_prefix (Path): Common prefix for the output files
        label (FDFLabel): Which FDF ``fdf_cube`` is ('dirty', 'clean', or 'model'), used to name the outputs
        threshold (float | None, optional): Amplitude cut applied before computing the moments. Defaults to None.

    Returns:
        list[Path]: The three written moment-map paths (mom0, mom1, mom2)
    """
    moments = calc_faraday_moments(
        fdf_cube,
        phi_arr_radm2=phi_arr_radm2,
        fwhm_rmsf_radm2=fwhm_rmsf_radm2,
        threshold=threshold,
    )
    header = WCS(reference_header).celestial.to_header()
    output_paths = []
    for moment_name, moment_map in zip(
        ("mom0", "mom1", "mom2"), (moments.mom0, moments.mom1, moments.mom2)
    ):
        output_path = Path(f"{output_prefix}.fdf.{label}.{moment_name}.fits")
        fits.writeto(output_path, np.asarray(moment_map, dtype=np.float32), header, overwrite=True)
        output_paths.append(output_path)
    return output_paths


def rmsynth_and_write_products(
    stokes_q_cube: Path,
    stokes_u_cube: Path,
    rmsynth_options: RMSynthOptions,
    rmclean_options: RMCleanOptions,
    cube_products: list[FDFLabel],
    moment_products: list[FDFLabel],
    output_prefix: Path,
    stokes_i_cube: Path | None = None,
) -> list[Path]:
    """Run RM-synthesis (and RM-CLEAN if needed) and write the requested output products.

    Args:
        stokes_q_cube (Path): Path to the Stokes Q FITS cube
        stokes_u_cube (Path): Path to the Stokes U FITS cube
        rmsynth_options (RMSynthOptions): Options controlling RM-synthesis
        rmclean_options (RMCleanOptions): Options controlling RM-CLEAN
        cube_products (list[FDFLabel]): Which FDF cube(s) to write ('dirty', 'clean', 'model')
        moment_products (list[FDFLabel]): Which FDF(s) to compute Faraday moment maps from
        output_prefix (Path): Common prefix for the output files
        stokes_i_cube (Path | None, optional): Path to a Stokes I FITS cube for the fractional-polarisation correction. Defaults to None.

    Returns:
        list[Path]: Every FITS path written
    """
    if not cube_products and not moment_products:
        logger.info("No RM-synthesis products requested, skipping.")
        return []

    synth_results = run_rmsynth_3d(
        stokes_q_cube=stokes_q_cube,
        stokes_u_cube=stokes_u_cube,
        rmsynth_options=rmsynth_options,
        stokes_i_cube=stokes_i_cube,
    )

    run_clean = any(label in ("clean", "model") for label in (*cube_products, *moment_products))
    clean_results = (
        run_rmclean_3d(rm_synth_results=synth_results, rmclean_options=rmclean_options)
        if run_clean
        else None
    )

    fdf_sources = {"dirty": synth_results.fdf_dirty_cube}
    if clean_results is not None:
        fdf_sources["clean"] = clean_results.clean_fdf_cube
        fdf_sources["model"] = clean_results.model_fdf_cube

    # Batch every lazy array needed by the requested products into a single
    # dask.compute call: computing per-product in a loop would redo the shared
    # synthesis/RM-CLEAN graph once per product instead of once total.
    needed_labels = {*cube_products, *moment_products}
    computed = dict(
        zip(needed_labels, dask.compute(*(fdf_sources[label] for label in needed_labels)))
    )

    reference_header = fits.getheader(stokes_q_cube)
    clean_moment_threshold = (
        rmclean_options.moment_threshold_snr * synth_results.theoretical_noise.fdf_error_noise
    )
    moment_thresholds: dict[FDFLabel, float | None] = {
        "dirty": None,
        "clean": clean_moment_threshold,
        "model": clean_moment_threshold,
    }

    output_paths: list[Path] = []
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
                fdf_cube=computed[label],
                phi_arr_radm2=synth_results.phi_arr_radm2,
                fwhm_rmsf_radm2=synth_results.fwhm_rmsf_radm2,
                reference_header=reference_header,
                output_prefix=output_prefix,
                label=label,
                threshold=moment_thresholds[label],
            )
        )

    return output_paths
