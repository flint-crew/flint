"""Helper utilities around the bespoke holography format of ASKAP"""

from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from capn_crunch import BaseOptions, add_options_to_parser, create_options_from_parser
from numpy.typing import NDArray
from reproject import reproject_interp
from reproject.mosaicking import find_optimal_celestial_wcs

from flint.logging import logger


class ConcatHolo(BaseOptions):
    """Options to use to concatenate holography cubes today"""

    out_path: Path
    """Output holography cube to make"""
    holo_cubes: tuple[Path, ...]
    """The path to the holography IQUV cubes to concatenate together"""


@dataclass
class FITSCubeInfo:
    """Slim container to represent a FITS cube"""

    path: Path
    """Path to the cube being considered"""
    header: fits.Header
    """Header from the FITS cube"""
    # data: NDArray[np.floating]
    # """The data attribute that corresponds to the header"""
    freqs_hz: NDArray[np.floating]
    """The minimum frequency in the cube (in Hertz, used for ordering)"""
    spatial_shape: tuple[int, int]
    """The (ny, nx) spatial shape of the input cubes"""
    celestial_wcs: WCS
    """The world coordinate system specification that accompanies the spatial dimension"""
    index: int = 0
    """The HDU index read"""


def get_freq_axis(header: fits.Header) -> NDArray[np.floating]:
    """Return array of channel centre frequencies (Hz)."""
    nfreq = header["NAXIS3"]
    crval = header["CRVAL3"]
    cdelt = header["CDELT3"]
    crpix = header["CRPIX3"]
    return crval + (np.arange(1, nfreq + 1) - crpix) * cdelt


def celestial_wcs_from_header(header: fits.Header) -> WCS:
    """Extract just the celestial (RA/Dec) 2D WCS from a 5D FITS header."""
    return WCS(header).celestial


def create_fits_info(cube_path: Path, hdu_index: int = 0) -> FITSCubeInfo:
    """Load a FITS cube container the holography and extra information
    from it.

    Args:
        cube_path (Path): Cube to load
        hdu_index (int, optional): The HDU the header and data are to be drawn from

    Returns:
        FITSCubeInfo: Representative information
    """
    hdu = fits.open(cube_path)
    header = hdu[hdu_index].header
    # data = hdu[0].data
    freqs_hz = get_freq_axis(header=header)

    spatial_shape = (header["NAXIS2"], header["NAXIS1"])
    celestial_wcs = celestial_wcs_from_header(header)

    return FITSCubeInfo(
        path=cube_path,
        header=header,
        # data=data,
        freqs_hz=freqs_hz,
        spatial_shape=spatial_shape,
        celestial_wcs=celestial_wcs,
        index=hdu_index,
    )


def load_and_sort_cubes(cube_paths: tuple[Path, ...]) -> list[FITSCubeInfo]:
    """Load and extra appropriate information from each input cube, and
    return a sorted list with increasing frequency

    Args:
        cube_paths (tuple[Path, ...]): Holography cubes to load

    Returns:
        list[FITSCubeInfo]: Sort list of cube information
    """
    logger.info(f"Considering {len(cube_paths)} inputs")

    # Load the cubes and then sort them in frequency order
    fits_cube_infos: list[FITSCubeInfo] = [
        create_fits_info(cube_path=cube_path) for cube_path in cube_paths
    ]
    fits_cube_infos.sort(key=lambda fits_cube_info: np.min(fits_cube_info.freqs_hz))

    logger.info("Input cubes (sorted by frequency):")
    for i, fits_cube_info in enumerate(fits_cube_infos):
        logger.info(f"  [{i}] {fits_cube_info.path.name}")
        logger.info(
            f"       {fits_cube_info.freqs_hz[0] / 1e6:.3f} – {fits_cube_info.freqs_hz[-1] / 1e6:.3f} MHz  ({len(fits_cube_info.freqs_hz)} channels)"
        )
        logger.info(
            f"       spatial: {fits_cube_info.header['NAXIS1']} x {fits_cube_info.header['NAXIS2']} px, "
        )

    return fits_cube_infos


def construct_spatial_output_wcs(fits_cube_infos: list[FITSCubeInfo]) -> fits.Header:
    """Construct the output WCS that will be used for each channel. The
    optimal WCS is find cia the ``reproject`` module.

    Args:
        fits_cube_infos (list[FITSCubeInfo]): The set of FITS cube

    Returns:
        fits.Header: The two-dimensional FITS header
    """

    wcs_out, shape_out_sky = find_optimal_celestial_wcs(
        [
            (fits_cube_info.spatial_shape, fits_cube_info.celestial_wcs)
            for fits_cube_info in fits_cube_infos
        ],
    )

    ny_out, nx_out = shape_out_sky

    logger.info(f"\nOptimal output spatial grid: {nx_out} x {ny_out} px")
    logger.info(f"  CDELT = {abs(wcs_out.wcs.cdelt[0]) * 3600:.4f} arcsec/px")

    # Build a minimal 2D FITS header for reproject_interp's output target
    out_header_2d = wcs_out.to_header()
    out_header_2d["NAXIS"] = 2
    out_header_2d["NAXIS1"] = nx_out
    out_header_2d["NAXIS2"] = ny_out

    return out_header_2d


@dataclass
class FrequencyGrid:
    """Representative frequency axis information"""

    cdelt: float
    """The CDELT field"""
    min_hz: float
    """The minimum frequency in Hz"""
    max_hz: float
    """The maximum frequency in Hz"""
    grid: NDArray[np.floating]
    """The final frequency grid"""


def construct_frequency_grid(fits_cube_infos: list[FITSCubeInfo]) -> FrequencyGrid:
    """Construct the final frequency grid of the concatenated holography cube

    Args:
        fits_cube_infos (list[FITSCubeInfo]): The sortted FITS cube meta data

    Raises:
        ValueError: Raised when the data do not appear in ascended order

    Returns:
        FrequencyGrid: The final frequency grid
    """

    if not all(
        np.diff([fits_cube_info.freqs_hz[0] for fits_cube_info in fits_cube_infos]) > 0
    ):
        msg = "Input FITS cube infors are not sorted in ascending frequency order"
        raise ValueError(msg)

    cdelt = fits_cube_infos[0].header["CDELT3"]
    freq_min = fits_cube_infos[0].freqs_hz[0]
    freq_max = fits_cube_infos[-1].freqs_hz[-1]
    n_out = round((freq_max - freq_min) / cdelt) + 1
    out_freqs = freq_min + np.arange(n_out) * cdelt

    logger.info(
        f"\nOutput freq grid: {freq_min / 1e6:.3f} – {freq_max / 1e6:.3f} MHz  "
        f"({n_out} channels, {cdelt / 1e6:.6f} MHz spacing)"
    )

    return FrequencyGrid(cdelt=cdelt, min_hz=freq_min, max_hz=freq_max, grid=out_freqs)


def map_frequencies_to_channels(
    freqs_1: np.NDArray[np.floating], freqs_2: NDArray[np.floating], tol: float
) -> tuple[NDArray[int], NDArray[int]]:
    """Create mappings resolving `freq_2` to channels in `freqs_1`

    Args:
        freqs_1 (np.NDArray[np.floating]): The final output frequency grid that the cube will be written to
        freqs_2 (NDArray[np.floating]): The frequency axis of the subject cube
        tol (float): The appropriate tolerance to use when mapping ``freqs_2`` onto ``freqs_1`` for a match to be found.

    Returns:
        tuple[NDArray[np.int], NDArray[np.int]]: The output channels into ``freqs_1``, and channel indices of ``freqs_2`` elements into ``freqs_1``
    """

    # Map this cube's input channels onto output frequency slots
    diffs = np.abs(freqs_1[:, None] - freqs_2[None, :])  # (n_out, n_in)
    ch_out = np.argmin(diffs, axis=0)
    matched = diffs[ch_out, np.arange(len(freqs_2))] < tol

    if not matched.all():
        logger.warning(
            f"  WARNING: {(~matched).sum()} channel(s) skipped (no matching output frequency slot)"
        )

    return ch_out, np.argwhere(matched)[0]


def _get_cube_header(fits_cube_info: FITSCubeInfo) -> fits.Header:
    """Return a header object from the input cube that is compatible with ``reproject``"""
    # Build a 2D spatial input header for reproject_interp
    in_header_2d = fits_cube_info.celestial_wcs.to_header()
    in_header_2d["NAXIS"] = 2
    in_header_2d["NAXIS1"] = fits_cube_info.header["NAXIS1"]
    in_header_2d["NAXIS2"] = fits_cube_info.header["NAXIS2"]

    return in_header_2d


def reproject_cubes(
    fits_cube_infos: list[FITSCubeInfo],
    spatial_header: fits.Header,
    frequency_grid: FrequencyGrid,
    cdelt_tol: float = 1e-6,
) -> NDArray[np.floating]:
    """Reproject the input cubes onto a final output spatial grid, as defined by
    ``spatial_grid``.

    Args:
        fits_cube_infos (list[FITSCubeInfo]): The input FITS cubes to me reprojected
        spatial_header (fits.Header): The description of the final output spatial grid data will be mapped to
        frequency_grid (FrequencyGrid): The frequency axis of the output cube
        cdelt_tol (float, optional): The acceptable tolerance between matching the frequency grid of input data to the output cube. Defaults to 1e-6.

    Returns:
        NDArray[np.floating]: The final reprojected array
    """

    # Axis order follows FITS convention reversed for numpy:
    # (NAXIS5=beam, NAXIS4=Stokes, NAXIS3=freq, NAXIS2=dec, NAXIS1=ra)
    nbeam = fits_cube_infos[0].header["NAXIS5"]
    nstokes = fits_cube_infos[0].header["NAXIS4"]
    out = np.full(
        (
            nbeam,
            nstokes,
            len(frequency_grid.grid),
            spatial_header["NAXIS2"],
            spatial_header["NAXIS1"],
        ),
        np.nan,
        dtype=np.float32,
    )
    logger.info(f"Output cube shape: {out.shape}")
    tol = (
        abs(frequency_grid.cdelt) * cdelt_tol
    )  # 1 ppm tolerance for frequency matching
    shape_out_sky = (spatial_header["NAXIS2"], spatial_header["NAXIS1"])

    for cube_idx, fits_cube_info in enumerate(fits_cube_infos):
        logger.info(f"Reprojecting cube {cube_idx} - {fits_cube_info.path} ...")

        logger.info("Loading data")
        arr = fits.getdata(
            fits_cube_info.path, fits_cube_info.index
        )  # (nbeam, nstokes, nchan, ny, nx)
        logger.info(f"Loaded data shape: {arr.shape}")
        ch_out, matched_indices = map_frequencies_to_channels(
            freqs_1=frequency_grid.grid, freqs_2=fits_cube_info.freqs_hz, tol=tol
        )
        in_cube_header = _get_cube_header(fits_cube_info=fits_cube_info)

        for beam in range(nbeam):
            for stokes in range(nstokes):
                for ch_idx in matched_indices:
                    plane = arr[beam, stokes, ch_idx, :, :]

                    reprojected, _ = reproject_interp(
                        (plane, in_cube_header),
                        spatial_header,
                        shape_out=shape_out_sky,
                    )
                    out[beam, stokes, ch_out[ch_idx], :, :] = reprojected.astype(
                        np.float32
                    )

            logger.info(
                f"  beam {beam + 1}/{nbeam} complete  "
                f"({len(matched_indices)} channels x {nstokes} Stokes planes)"
            )

    return out


def create_output_header(
    base_header: fits.Header, spatial_header: fits.Header, frequency_grid: FrequencyGrid
) -> fits.Header:
    """Create the final outcube cube header. This uses the header from an example
    FITS cube as a base, and applies appropriate keywords to indicate the new
    spatial and frequency dimensions

    Args:
        base_header (fits.Header): The header to use a base to update from
        spatial_header (fits.Header): The optimal spatial header consistent across all input cubes
        frequency_grid (FrequencyGrid): The frequency axis to embed

    Returns:
        fits.Header: The final output header
    """

    logger.info("Creating final output header")
    out_header = base_header.copy()

    for key, value in spatial_header.items():
        out_header[key] = value

    out_header["NAXIS3"] = len(frequency_grid.grid)
    out_header["CRVAL3"] = frequency_grid.min_hz
    out_header["CRPIX3"] = 1.0
    out_header["CDELT3"] = frequency_grid.cdelt

    return out_header


def concatenate_holography(concat_holo_options: ConcatHolo) -> Path:
    """Reproject a set of ASKAP IQUV primary beam cubes into a single output cube.
    An optimal spatial grid is computed internally through ``reproject``, and input
    cubes are placed onto a consistent channel frequency gride.

    Args:
        concat_holo_options (ConcatHolo): Options to direction the concatenation of the holography cubes.

    Returns:
        Path: Path to the output cube formed
    """

    logger.info("Attempting to concatenate holography cubes")

    fits_cube_infos = load_and_sort_cubes(cube_paths=concat_holo_options.holo_cubes)
    spatial_header = construct_spatial_output_wcs(fits_cube_infos=fits_cube_infos)
    frequency_grid = construct_frequency_grid(fits_cube_infos=fits_cube_infos)

    reprojected_holo = reproject_cubes(
        fits_cube_infos=fits_cube_infos,
        spatial_header=spatial_header,
        frequency_grid=frequency_grid,
        cdelt_tol=1e-6,  # 1ppm of the cdelt
    )
    output_header = create_output_header(
        base_header=fits_cube_infos[0].header,
        spatial_header=spatial_header,
        frequency_grid=frequency_grid,
    )

    logger.info(
        f"Writing out data of shape {reprojected_holo.shape} to {concat_holo_options.out_path}"
    )
    fits.PrimaryHDU(data=reprojected_holo, header=output_header).writeto(
        concat_holo_options.out_path, overwrite=True
    )

    return concat_holo_options.out_path


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Helper utilities around holography")

    parser = add_options_to_parser(parser=parser, options_class=ConcatHolo)

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    concat_holo_options = create_options_from_parser(
        parser_namespace=args, options_class=ConcatHolo
    )

    concatenate_holography(concat_holo_options=concat_holo_options)


if __name__ == "__main__":
    cli()
