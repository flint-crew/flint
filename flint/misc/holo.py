"""Helper utilities around the bespoke holography format of ASKAP"""

from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from socket import gethostname
from typing import NamedTuple

import billiard
import numpy as np
from astropy import constants as const
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS
from capn_crunch import BaseOptions, add_options_to_parser, create_options_from_parser
from numpy.typing import NDArray
from reproject import reproject_interp
from reproject.mosaicking import find_optimal_celestial_wcs
from scipy.ndimage import map_coordinates

from flint.exceptions import UnusableBeamError
from flint.logging import logger


class ConcatHoloOptions(BaseOptions):
    """Options to use to concatenate holography cubes today"""

    output_path: Path | None = None
    """Output holography cube to make"""
    holo_cubes: tuple[Path, ...] = ()
    """The path to the holography IQUV cubes to concatenate together. No sensible
    default exists (it always names specific input cubes); an empty tuple is a
    placeholder that must be overridden before calling ``concatenate_holography``,
    which raises if it is left unset"""
    max_workers: int = 8
    """The maximum number of sub-processes that may be spawned"""


@dataclass
class FITSCubeInfo:
    """Slim container to represent a FITS cube"""

    path: Path
    """Path to the cube being considered"""
    header: fits.Header
    """Header from the FITS cube"""
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


class BeamResponse(NamedTuple):
    """One beam's response from a holography cube, normalised to 1 at the beam centre"""

    freqs_hz: NDArray[np.floating]
    """Frequency of each usable plane"""
    planes: NDArray[np.floating]
    """Response on each usable plane, shaped (freq, y, x)"""
    pixel_scale_rad: tuple[float, float]
    """Signed pixel size along x and y, in radians (CDELT1, CDELT2)"""
    centre_pixel: tuple[float, float]
    """Zero-based (x, y) pixel of the beam centre"""


def _sample_planes(
    planes: NDArray[np.floating], x: float, y: float
) -> NDArray[np.floating]:
    """Bilinearly interpolate every plane at one pixel position, giving 0 off the grid."""
    nfreq = planes.shape[0]
    coords = np.array([np.arange(nfreq), np.full(nfreq, y), np.full(nfreq, x)])
    return map_coordinates(planes, coords, order=1, mode="constant", cval=0.0)


def _direction_cosines(centre: SkyCoord, position: SkyCoord) -> tuple[float, float]:
    """Offset of `position` from `centre` as direction cosines (l to the east, m to the north)."""
    ra0, dec0 = centre.icrs.ra.rad, centre.icrs.dec.rad
    ra, dec = position.icrs.ra.rad, position.icrs.dec.rad
    east = np.cos(dec) * np.sin(ra - ra0)
    north = np.sin(dec) * np.cos(dec0) - np.cos(dec) * np.sin(dec0) * np.cos(ra - ra0)
    return float(east), float(north)


def _exit_scale(response: BeamResponse, dx: float, dy: float) -> float:
    """Fraction of the pixel offset (dx, dy) from the beam centre that stays a pixel inside the cube."""
    ny, nx = response.planes.shape[1:]
    x0, y0 = response.centre_pixel
    room_x = nx - 2 - x0 if dx > 0 else x0 - 1
    room_y = ny - 2 - y0 if dy > 0 else y0 - 1
    return min(
        room_x / abs(dx) if dx else np.inf,
        room_y / abs(dy) if dy else np.inf,
    )


def load_beam_response(holofile: Path, beam: int, stokes: int = 0) -> BeamResponse:
    """Read one beam's response from a holography cube, normalised to 1 at the beam centre.

    As in linmos, the spatial axes are offsets about the beam centre: only CRPIX
    and CDELT are used, never CRVAL. NaNs count as no response, and planes with
    no response at the beam centre (such as flagged channels) are dropped. The
    usable planes are returned in increasing frequency.

    Args:
        holofile (Path): Holography cube with axes (beam, stokes, freq, dec, ra)
        beam (int): ASKAP beam number (0-indexed) to read
        stokes (int, optional): Stokes index to read. Defaults to 0 (Stokes I).

    Raises:
        ValueError: Raised if the beam centre (CRPIX) is within a pixel of the cube's edge
        UnusableBeamError: Raised if the beam has no usable planes

    Returns:
        BeamResponse: The beam's normalised response on its usable planes
    """
    with fits.open(holofile, memmap=True) as hdul:
        header = hdul[0].header
        planes = np.nan_to_num(np.asarray(hdul[0].data[beam, stokes], dtype=float))

    ny, nx = planes.shape[1:]
    x0, y0 = header["CRPIX1"] - 1, header["CRPIX2"] - 1
    if min(x0, nx - 1 - x0, y0, ny - 1 - y0) < 2:
        raise ValueError(
            f"The beam centre (CRPIX) of {holofile} is at pixel ({x0 + 1}, {y0 + 1}) "
            f"of a {nx}x{ny} grid, within a pixel of its edge. ASKAP holography "
            "cubes are centred on the beam."
        )

    centre = _sample_planes(planes, x0, y0)
    usable = centre > 0
    if not np.any(usable):
        raise UnusableBeamError(f"Beam {beam} of {holofile} has no usable response")
    logger.info(f"Using {np.sum(usable)} of {len(usable)} planes of beam {beam}")

    freqs_hz = get_freq_axis(header=header)[usable]
    order = np.argsort(freqs_hz)

    return BeamResponse(
        freqs_hz=freqs_hz[order],
        planes=(planes[usable] / centre[usable, None, None])[order],
        pixel_scale_rad=(
            float(np.radians(header["CDELT1"])),
            float(np.radians(header["CDELT2"])),
        ),
        centre_pixel=(float(x0), float(y0)),
    )


def outside_planes(response: BeamResponse, freqs: u.Quantity) -> NDArray[np.bool_]:
    """Which `freqs` lie more than half a plane beyond the usable planes, and so take the nearest plane's beam."""
    freqs_hz = freqs.to(u.Hz).value
    plane_freqs = response.freqs_hz
    half_plane = np.min(np.diff(plane_freqs)) / 2 if len(plane_freqs) > 1 else 0.0
    return (freqs_hz < plane_freqs[0] - half_plane) | (
        freqs_hz > plane_freqs[-1] + half_plane
    )


def sample_beam_response(
    response: BeamResponse,
    beam_centre: SkyCoord,
    position: SkyCoord,
    freqs: u.Quantity,
    alpha: float = 0.0,
    taper: bool = False,
) -> NDArray[np.floating]:
    """Sample a beam's response at a sky position the same way linmos does.

    The offset of `position` from `beam_centre` is rotated by `alpha` before it
    is looked up, as linmos does with `ASKAP_PB.alpha`. Positions off the cube
    give 0 unless `taper` is set. With `taper` set, a position off the cube
    takes the value where the line from the beam centre through it leaves the
    cube (a pixel inside the edge), falling off past there like a Gaussian beam.

    Args:
        response (BeamResponse): The beam to sample, from `load_beam_response`
        beam_centre (SkyCoord): Direction of the beam centre, normally the MS phase centre
        position (SkyCoord): Sky position to sample at
        freqs (u.Quantity): Frequencies to interpolate the response onto
        alpha (float, optional): Rotation in radians, from
            `flint.coadd.linmos.compute_pb_rotation_alpha`. Defaults to 0.0.
        taper (bool, optional): Continue the response past the edge of the cube. Defaults to False.

    Returns:
        NDArray[np.floating]: Response at each of `freqs`
    """
    east, north = _direction_cosines(centre=beam_centre, position=position)
    x = east * np.cos(alpha) - north * np.sin(alpha)
    y = east * np.sin(alpha) + north * np.cos(alpha)

    dx, dy = x / response.pixel_scale_rad[0], y / response.pixel_scale_rad[1]
    scale = _exit_scale(response=response, dx=dx, dy=dy)
    beyond_edge = taper and scale < 1
    if beyond_edge:
        dx, dy = dx * scale, dy * scale

    spectrum = _sample_planes(
        response.planes, dx + response.centre_pixel[0], dy + response.centre_pixel[1]
    )
    atten = np.interp(freqs.to(u.Hz).value, response.freqs_hz, spectrum)

    if beyond_edge:
        # How much a Gaussian beam of FWHM lambda/D, for a 12 m dish, falls
        # between where the line leaves the cube and the position
        radius = np.hypot(x, y)
        fwhm = (const.c / freqs / (12.0 * u.m)).decompose().value
        offset, edge = np.arcsin(radius), np.arcsin(radius * scale)
        falloff = np.exp(-4 * np.log(2) * (offset**2 - edge**2) / fwhm**2)
        atten = np.clip(atten, 0.0, None) * falloff

    return atten


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
) -> tuple[NDArray[np.int_], NDArray[np.int_]]:
    """Create mappings resolving `freq_2` to channels in `freqs_1`

    The outputs here are channel indices. The first array are the indices into
    the `freqs_1` grid. The second set are the the elements of ``freqs_2`` that
    were successfully matched.

    Args:
        freqs_1 (np.NDArray[np.floating]): The final output frequency grid that the cube will be written to
        freqs_2 (NDArray[np.floating]): The frequency axis of the subject cube
        tol (float): The appropriate tolerance to use when mapping ``freqs_2`` onto ``freqs_1`` for a match to be found.

    Returns:
        tuple[NDArray[np.int_], NDArray[np.int_]]: The output channels into ``freqs_1``, and channel indices of ``freqs_2`` elements into ``freqs_1``
    """

    # Map this cube's input channels onto output frequency slots
    diffs = np.abs(freqs_1[:, None] - freqs_2[None, :])  # (n_out, n_in)
    ch_out = np.argmin(diffs, axis=0)
    matched = diffs[ch_out, np.arange(len(freqs_2))] < tol

    if not matched.all():
        logger.warning(
            f"  WARNING: {(~matched).sum()} channel(s) skipped (no matching output frequency slot)"
        )

    return ch_out, np.where(matched)[0]


def _get_cube_header(fits_cube_info: FITSCubeInfo) -> fits.Header:
    """Return a header object from the input cube that is compatible with ``reproject``"""
    # Build a 2D spatial input header for reproject_interp
    in_header_2d = fits_cube_info.celestial_wcs.to_header()
    in_header_2d["NAXIS"] = 2
    in_header_2d["NAXIS1"] = fits_cube_info.header["NAXIS1"]
    in_header_2d["NAXIS2"] = fits_cube_info.header["NAXIS2"]

    return in_header_2d


@dataclass
class ReprojectWorker:
    in_cube_path: Path
    """The cube to load"""
    in_ch_idx: int
    """The channel index of the input cube"""
    in_cube_header: fits.Header
    """The header that corresponds to ``plane``"""
    beam: int
    """The beam index"""
    stokes: int
    """The stokes index"""
    out_idx: int
    """The channel index"""
    final_fits_cube_info: FinalFITSCubeInfo
    """Information around the destination FITS cube"""


def reproject_wrapper(
    reproject_worker: ReprojectWorker,
    output_projection: fits.Header,
    shape_out: tuple[int, int],
) -> ReprojectWorker:
    in_cube_arr = fits.getdata(reproject_worker.in_cube_path, memmap=True)

    plane = in_cube_arr[
        reproject_worker.beam,
        reproject_worker.stokes,
        reproject_worker.in_ch_idx,
        :,
        :,
    ]
    reprojected, _ = reproject_interp(
        (plane, reproject_worker.in_cube_header),
        output_projection=output_projection,
        shape_out=shape_out,
    )

    dest_cube_info: FinalFITSCubeInfo = reproject_worker.final_fits_cube_info

    arr = np.memmap(
        filename=dest_cube_info.path,
        dtype=">f4",
        offset=dest_cube_info.data_offset,
        mode="r+",
        shape=dest_cube_info.output_shape,
    )
    arr[
        reproject_worker.beam,
        reproject_worker.stokes,
        reproject_worker.out_idx,
        :,
        :,
    ] = reprojected.astype(">f4")

    # NOTE: We are relying on the operating system to flush the updated data back
    # to disk as it sees fit. The Usage of memmap here is pointing to a single
    # block of memory between processes, and the OS is smart enough to manage
    # this similar to what it would otherwise do with virtual memory -> page file.
    return reproject_worker


def get_output_data_shape(
    fits_cube_infos: list[FITSCubeInfo],
    spatial_header: fits.Header,
    frequency_grid: FrequencyGrid,
) -> list[int]:
    """Construct the final output shape of the concatenated holography cube

    Args:
        fits_cube_infos (list[FITSCubeInfo]): The loaded information from the individual FITS cubes
        spatial_header (fits.Header): The new output spatial header information
        frequency_grid (FrequencyGrid): The output frequency grid

    Returns:
        list[int]: The expected output shape of the new holography
    """

    nbeam = fits_cube_infos[0].header["NAXIS5"]
    nstokes = fits_cube_infos[0].header["NAXIS4"]
    out_shape = [
        nbeam,
        nstokes,
        len(frequency_grid.grid),
        spatial_header["NAXIS2"],
        spatial_header["NAXIS1"],
    ]

    logger.info(f"Output data shape: {out_shape}")
    return out_shape


@dataclass
class FinalFITSCubeInfo:
    """Simple representation of final FITS cube"""

    path: Path
    """The output path of the fits file"""
    output_shape: list[int]
    """The output shape of the data"""
    data_offset: int
    """The number of bytes the FITS header represents"""


def create_placeholder_cube(
    fits_cube_infos: list[FITSCubeInfo],
    spatial_header: fits.Header,
    frequency_grid: FrequencyGrid,
    output_path: Path,
) -> FinalFITSCubeInfo:
    """Write out the final fits cube, but fill it with placeholder
    data.

    Args:
        fits_cube_infos (list[FITSCubeInfo]): The set of loaded FITS cubes
        spatial_header (fits.Header): The new spatial header description
        frequency_grid (FrequencyGrid): The output frequency grid
        output_path (Path): The output path to write the cube to

    Returns:
        FinalFITSCubeInfo: The output cube written
    """
    logger.info("Creating placeholder FITS cube file")
    output_header = create_output_header(
        base_header=fits_cube_infos[0].header,
        spatial_header=spatial_header,
        frequency_grid=frequency_grid,
    )
    # TODO: This should be rewritten to extract purely from
    # the output header
    output_shape = get_output_data_shape(
        fits_cube_infos=fits_cube_infos,
        spatial_header=spatial_header,
        frequency_grid=frequency_grid,
    )
    logger.info("Generating placeholder data")

    data_payload_size = np.prod(output_shape) * 4  # 4 bytes for float32
    with open(output_path, "wb") as f:
        f.write(output_header.tostring().encode("ascii"))
        data_offset = f.tell()  # End of header, start of data

        # NOTE: Seeking to this position creates the output file on
        # disk for later use. It will be all zeros, and it is done
        # instantly. This works on linux systemss, may not on Windows
        # or OSX - the latter had a unit test failing when attempting
        # to fits.getdata on it, returning a buffer vs data shape mismatch.
        # Curious, pal.
        f.seek(data_payload_size - 1, 1)  # The -1 makes space for empty byte below
        f.write(b"\x00")  # Writing out a byte fixes OSX issue

    logger.info(f"Start of data: {data_offset} bytes")
    logger.info(f"Size of data: {data_payload_size} bytes")

    return FinalFITSCubeInfo(
        path=output_path,
        output_shape=output_shape,
        data_offset=data_offset,
    )


def reproject_cubes(
    fits_cube_infos: list[FITSCubeInfo],
    spatial_header: fits.Header,
    frequency_grid: FrequencyGrid,
    final_fits_cube_info: FinalFITSCubeInfo,
    cdelt_tol: float = 1e-6,
    max_workers: int = 2,
) -> FinalFITSCubeInfo:
    """Reproject the input cubes onto a final output spatial grid, as defined by
    ``spatial_grid``.

    Args:
        fits_cube_infos (list[FITSCubeInfo]): The input FITS cubes to me reprojected
        spatial_header (fits.Header): The description of the final output spatial grid data will be mapped to
        frequency_grid (FrequencyGrid): The frequency axis of the output cube
        cdelt_tol (float, optional): The acceptable tolerance between matching the frequency grid of input data to the output cube. Defaults to 1e-6.
        final_fits_cube_info (FinalFITSCubeInfo): The description of the final output cube

    Returns:
        FinalFITSCubeInfo: The final FITS cube info that was provided as input
    """

    # Axis order follows FITS convention reversed for numpy:
    # (NAXIS5=beam, NAXIS4=Stokes, NAXIS3=freq, NAXIS2=dec, NAXIS1=ra)
    nbeam = final_fits_cube_info.output_shape[0]
    nstokes = final_fits_cube_info.output_shape[1]

    logger.info(f"Output cube shape: {final_fits_cube_info.output_shape}")
    tol = (
        abs(frequency_grid.cdelt) * cdelt_tol
    )  # 1 ppm tolerance for frequency matching
    shape_out_sky = (spatial_header["NAXIS2"], spatial_header["NAXIS1"])

    # Using billiard Pool to allow a process pool executor like parallelism to be
    # available when running under a dask-worker context. In such cases sub-processes
    # may not normally be spawned - dask-workers start as a daemon and can not have
    # children.
    with billiard.Pool(max_workers) as pool:
        for cube_idx, fits_cube_info in enumerate(fits_cube_infos):
            logger.info(f"Reprojecting cube {cube_idx} - {fits_cube_info.path} ...")

            ch_out, matched_indices = map_frequencies_to_channels(
                freqs_1=frequency_grid.grid, freqs_2=fits_cube_info.freqs_hz, tol=tol
            )
            in_cube_header = _get_cube_header(fits_cube_info=fits_cube_info)

            reproject_interp_func = partial(
                reproject_wrapper,
                output_projection=spatial_header,
                shape_out=shape_out_sky,
            )
            for beam in range(nbeam):
                pool_items = []
                for stokes in range(nstokes):
                    for ch_idx in matched_indices:
                        pool_items.append(
                            ReprojectWorker(
                                in_cube_path=fits_cube_info.path,
                                in_ch_idx=ch_idx,
                                in_cube_header=in_cube_header,
                                beam=beam,
                                stokes=stokes,
                                out_idx=ch_out[ch_idx],
                                final_fits_cube_info=final_fits_cube_info,
                            )
                        )

                logger.debug(f"Collcted {len(pool_items)} for reprojection")
                results = pool.map(reproject_interp_func, pool_items, chunksize=8)

                for reproject_worker in results:
                    logger.debug(
                        f"Finished {reproject_worker.beam} {reproject_worker.stokes}"
                    )

                logger.info(
                    f"  beam {beam + 1}/{nbeam} complete  "
                    f"({len(matched_indices)} channels x {nstokes} Stokes planes)"
                )

    return final_fits_cube_info


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
    out_header["NAXIS"] = 5

    return out_header


def concatenate_holography(concat_holo_options: ConcatHoloOptions) -> Path:
    """Reproject a set of ASKAP IQUV primary beam cubes into a single output cube.
    An optimal spatial grid is computed internally through ``reproject``, and input
    cubes are placed onto a consistent channel frequency gride.

    Args:
        concat_holo_options (ConcatHoloOptions): Options to direction the concatenation of the holography cubes.

    Returns:
        Path: Path to the output cube formed
    """

    if concat_holo_options.output_path is None or not concat_holo_options.holo_cubes:
        raise ValueError(
            "concat_holo_options.output_path and holo_cubes are placeholder "
            f"defaults and must be set, got {concat_holo_options=}"
        )

    logger.info("Attempting to concatenate holography cubes")
    logger.info(f"Running on host {gethostname()}")

    fits_cube_infos = load_and_sort_cubes(cube_paths=concat_holo_options.holo_cubes)
    spatial_header = construct_spatial_output_wcs(fits_cube_infos=fits_cube_infos)
    frequency_grid = construct_frequency_grid(fits_cube_infos=fits_cube_infos)

    final_fits_cube_info: FinalFITSCubeInfo = create_placeholder_cube(
        fits_cube_infos=fits_cube_infos,
        spatial_header=spatial_header,
        frequency_grid=frequency_grid,
        output_path=concat_holo_options.output_path,
    )

    reproject_cubes(
        fits_cube_infos=fits_cube_infos,
        spatial_header=spatial_header,
        frequency_grid=frequency_grid,
        cdelt_tol=1e-6,  # 1ppm of the cdelt
        final_fits_cube_info=final_fits_cube_info,
        max_workers=concat_holo_options.max_workers,
    )

    logger.info(f"Finished writing {concat_holo_options.output_path}")
    return concat_holo_options.output_path


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Helper utilities around holography")

    parser = add_options_to_parser(parser=parser, options_class=ConcatHoloOptions)

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    concat_holo_options = create_options_from_parser(
        parser_namespace=args, options_class=ConcatHoloOptions
    )

    concatenate_holography(concat_holo_options=concat_holo_options)


if __name__ == "__main__":
    cli()
