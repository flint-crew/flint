"""Construct a leakge map between two polarisations, typically V/I"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import NamedTuple, TypeAlias

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.table import Table
from astropy.wcs import WCS
from astropy.wcs.utils import skycoord_to_pixel

from flint.catalogue import guess_column_in_table
from flint.logging import logger

TableOrPath: TypeAlias = Table | Path


class LeakageFilters(NamedTuple):
    """Description of the filtering options to apply to components
    when characterising leakage"""

    isolation_radius_deg: float = 0.0155
    """The minimum distance to the nearest component"""
    upper_int_peak_ratio: float = 1.2
    """The upper limit on acceptable int/peak ratios"""
    lower_int_peak_ratio: float = 0.8
    """The lower limit on acceptable int/peak ratios"""
    search_box_size: int = 1
    """The size of a box to search for peak polarised signal in"""
    noise_box_size: int = 30
    """the size of a box to compute a local RMS noise measure from"""
    mean_box_size: int = 10
    """The size of abox to compute a local mean measure over"""
    source_snr: float = 40
    """Minimum stokes-I signal-to-noise ratio"""


class FITSImage(NamedTuple):
    """Container to couple FITS header, image and WCS"""

    data: np.ndarray
    """The data of the fits image"""
    header: dict
    """Header of the fits image"""
    wcs: WCS
    """Celestial WCS of the fits image"""
    path: Path
    """Path of the loaded FITS image on disk"""


class PixelCoords(NamedTuple):
    """Slim container to help collect and maintain pixel coordinates. Not intended for extensive use"""

    y: np.ndarray
    """The y-coordinate of a set of pixels"""
    x: np.ndarray
    """The x-coordinate of a set of pixels"""


def _load_fits_image(fits_path: Path) -> FITSImage:
    """Load in a FITS image and package the components into a consistent
    form. Not intended for extensive use.

    Args:
        fits_path (Path): The path of the FITS image to examining

    Returns:
        FITSImage: Loaded FITS properties
    """

    assert fits_path.suffix == ".fits", (
        f"Unexpected file type for {fits_path=}, expected fits"
    )
    logger.info(f"Opening {fits_path=}")
    with fits.open(fits_path) as in_fits:
        image_data = in_fits[0].data  # type: ignore
        header = dict(in_fits[0].header.items())  # type: ignore
        wcs = WCS(header)

    return FITSImage(data=image_data, header=header, wcs=wcs, path=fits_path)


def _load_component_table(catalogue: TableOrPath) -> Table:
    """Return a table given either a loaded table or a path to a table on disk"""
    component_table = (
        Table.read(catalogue) if isinstance(catalogue, Path) else catalogue
    )
    logger.info(f"Loaded component table of {len(component_table)}")

    return component_table


def filter_components(
    table: Table,
    peak_col: str,
    int_col: str,
    int_err_col: str,
    leakage_filters: LeakageFilters,
    ra_col: str | None = None,
    dec_col: str | None = None,
) -> Table:
    """Apply the pre-processing operations to catalogue components to select an
    optimal sample of sources for leakage characterisation. Sources will be selected
    based on:

    * how isolated they are
    * compactness, as traced by their int/peak

    Args:
        table (Table): Collection of sources, as produced from a source finder
        peak_col (str): The column name describing the peak flux density
        int_col (str): The column name describing integrated flux
        int_err_col (str): The column container errors that correspond to `int_col` to use when computing signal-to-noise
        leakage_filters (LeakageFilters): Criteria applied to the source components in the table
        ra_col (Optional[str], optional): The RA column name. If None, it will be guessed. Defaults to None.
        dec_col (Optional[str], optional): The Dec column name. If None, it will be guessed. Defaults to None.

    Returns:
        Table: A filtered table
    """
    ra_col = guess_column_in_table(table=table, column="ra", guess_column=ra_col)
    dec_col = guess_column_in_table(table=table, column="dec", guess_column=dec_col)

    assert all(
        [
            col in table.colnames
            for col in (ra_col, dec_col, peak_col, int_col, int_err_col)
        ]
    ), (
        f"Supplied column names {ra_col=} {dec_col=} {peak_col=} {int_col=} partly missing from {table.colnames=}"
    )

    total_comps = len(table)
    sky_coords = SkyCoord(table[ra_col], table[dec_col], unit=(u.deg, u.deg))

    # The match_to_catalog_sky return idx, sep2d, sep3d. We care about separation, matey
    isolation_mask = sky_coords.match_to_catalog_sky(sky_coords, nthneighbor=2)[1] > (
        leakage_filters.isolation_radius_deg * u.deg
    )  # type: ignore
    logger.info(
        f"{np.sum(isolation_mask)} of {total_comps} sources are isolated with radius {float(leakage_filters.isolation_radius_deg):.5f} deg"
    )

    ratio = table[int_col] / table[peak_col]  # type: ignore
    ratio_mask = (leakage_filters.lower_int_peak_ratio < ratio) & (
        ratio < leakage_filters.upper_int_peak_ratio
    )  # type: ignore
    logger.info(
        f"{np.sum(ratio_mask)} of {total_comps} sources are compact with {leakage_filters.lower_int_peak_ratio} < int/peak < {leakage_filters.upper_int_peak_ratio}"
    )

    signal_to_noise = table[int_col] / table[int_err_col]  # type: ignore
    signal_mask = signal_to_noise > leakage_filters.source_snr
    logger.info(
        f"{np.sum(signal_mask)} of {total_comps} sources have S/N above {leakage_filters.source_snr}"
    )

    mask = isolation_mask & ratio_mask & signal_mask
    logger.info(
        f"{np.sum(mask)} of {total_comps} sources are isolated, compact and bright"
    )

    table = table[mask]  # type: ignore

    return table


def get_xy_pixel_coords(
    table: Table,
    wcs: WCS,
    ra_col: str | None = None,
    dec_col: str | None = None,
) -> PixelCoords:
    """Convert (RA, Dec) positions in a catalogue into (x, y)-pixels given an WCS

    Args:
        table (Table): The table containing sources to collect (x, y)-coordinates
        wcs (WCS): The WCS description to use to resolve (RA, Dec) to (x, y)
        ra_col (Optional[str], optional): The RA column name. If None, it will be guessed. Defaults to None.
        dec_col (Optional[str], optional): The Dec column name. If None, it will be guessed. Defaults to None.

    Returns:
        PixelCoords: _description_
    """
    ra_col = guess_column_in_table(table=table, column="ra")
    dec_col = guess_column_in_table(table=table, column="dec")

    sky_coord = SkyCoord(table[ra_col], table[dec_col], unit=(u.deg, u.deg))
    x, y = skycoord_to_pixel(wcs=wcs.celestial, coords=sky_coord, origin=0)

    return PixelCoords(y=np.floor(y).astype(int), x=np.floor(x).astype(int))


def load_and_filter_components(
    catalogue: TableOrPath, leakage_filters: LeakageFilters
) -> Table:
    """Load in a component catalogue table and apply filters to them. The
    remaining components will be used to characterise leakage

    Args:
        catalogue (TableOrPath): The path to a component catalogue, or a loaded component catalogue
        leakage_filters (LeakageFilters): Filtering options to find ideal components for leakage characterisation

    Returns:
        Table: Filtered component catalogue
    """
    # TODO: Need to have a single guess function here
    comp_table = _load_component_table(catalogue=catalogue)
    ra_col = guess_column_in_table(table=comp_table, column="ra")
    dec_col = guess_column_in_table(table=comp_table, column="dec")
    peak_col = guess_column_in_table(table=comp_table, column="peakflux")
    int_col = guess_column_in_table(table=comp_table, column="intflux")
    int_err_col = guess_column_in_table(table=comp_table, column="intfluxerr")

    # TODO: Need to use Catalogue here
    comp_table = filter_components(
        table=comp_table,
        ra_col=ra_col,
        dec_col=dec_col,
        peak_col=peak_col,
        int_col=int_col,
        int_err_col=int_err_col,
        leakage_filters=leakage_filters,
    )
    return comp_table


class PolStatistics(NamedTuple):
    """Simple container for statistics around the extraction of leakage polarisation statistics"""

    peak: np.ndarray
    """The peak pixel value"""
    noise: np.ndarray
    """The standard deviation of pixels within a box"""
    mean: np.ndarray
    """The mean of pixels within a box"""


def extract_pol_stats_in_box(
    pol_image: np.ndarray,
    pixel_coords: PixelCoords,
    search_box_size: int,
    noise_box_size: int,
    mean_box_size: int,
) -> PolStatistics:
    """Construct two boxes around nominated pixel coordinates to:

    * extract the peak signal within
    * calculate a local RMS value for

    Args:
        pol_image (np.ndarray): The loaded polarised image
        pixel_coords (PixelCoords): Collection of pixel positioncs to evaluate the peak polarisation and noise at
        search_box_size (int): Size of box to extract the maximum polarised signal from
        noise_box_size (int): Size of box to calculate the RMS over
        mean_box_size (int): Size of box to calculate an mean over

    Returns:
       PolStatistics: Extracted statistics, including peak polarised signal, noise and mean
    """
    y_max, x_max = pol_image.shape[-2:]

    logger.info(f"{pol_image.shape=}, extracted {y_max=} and {x_max=}")

    pol_peak = None
    pol_noise = None
    pol_mean = None
    # TODO: This loop should be reformed to allow iterating over something that defines the mode and box
    for idx, box_size in enumerate((search_box_size, noise_box_size, mean_box_size)):
        box_delta = int(np.ceil(box_size / 2))

        y_edge_min = np.maximum(pixel_coords.y - box_delta, 0).astype(int)
        y_edge_max = np.minimum(pixel_coords.y + box_delta, y_max - 1).astype(int)

        x_edge_min = np.maximum(pixel_coords.x - box_delta, 0).astype(int)
        x_edge_max = np.minimum(pixel_coords.x + box_delta, x_max - 1).astype(int)

        assert not np.any(y_edge_min == y_edge_max), "The y box edges are equal"
        assert not np.any(x_edge_min == x_edge_max), "The x box edges are equal"

        pol_image = np.squeeze(pol_image)

        # search_box = np.squeeze(pol_image)[y_edge_min:y_edge_max, x_edge_min:x_edge_max]
        search_box = [
            pol_image[y_min:y_max, x_min:x_max].flatten()
            for (y_min, y_max, x_min, x_max) in zip(
                y_edge_min, y_edge_max, x_edge_min, x_edge_max
            )
        ]

        if idx == 0:
            pol_peak = np.array(
                [
                    (
                        data[np.nanargmax(np.abs(data))]
                        if np.any(np.isfinite(data))
                        else np.nan
                    )
                    for data in search_box
                ]
            )
        elif idx == 1:
            pol_noise = np.array(
                [
                    np.nanstd(data) if np.any(np.isfinite(data)) else np.nan
                    for data in search_box
                ]
            )
        elif idx == 2:
            pol_mean = np.array(
                [
                    np.nanmean(data) if np.any(np.isfinite(data)) else np.nan
                    for data in search_box
                ]
            )

    assert pol_peak is not None, f"{pol_peak=}, which should not happen"
    assert pol_noise is not None, f"{pol_noise=}, which should not happen"
    assert pol_mean is not None, f"{pol_mean=}, which should not happen"

    pol_stats = PolStatistics(peak=pol_peak, noise=pol_noise, mean=pol_mean)

    return pol_stats


def _get_output_catalogue_path(
    input_path: Path, pol: str, output_path: Path | None = None
) -> Path:
    """Create the output leakage catalogue name"""
    # NOTE: This is a separate function to test against after a silly. Might move with the other named Pirates
    assert isinstance(input_path, Path)
    input_suffix = input_path.suffix

    output_path = (
        input_path.with_suffix(f".{pol}_leakage{input_suffix}")
        if output_path is None
        else output_path
    )
    assert output_path is not None, (
        f"{output_path=} is empty, and no catalogue path provided"
    )

    return Path(output_path)


def create_leakge_component_table(
    pol_image: Path,
    catalogue: Table | Path,
    pol: str = "v",
    output_path: Path | None = None,
) -> Path:
    """Create a component catalogue that includes enough information to describe the
    polarisation fraction of sources across a field. This is intended to be used
    for leakage characterisation.

    New catalogue columns will be added:

    * pol_fraction: The POL/I fraction. The peak flux is taken from the catalogue, using the appropriate column name
    * pol_peak: The peak polarised signal in the nearby region of a component position
    * pol_noise: The noise in the polarised image pixels around the component position

    Args:
        pol_image (Path): The polarised image that will be used to extract peak polarised flux from
        catalogue (Union[Table, Path]): Component table describing positions to extract flux from
        pol (str, optional): The polarisation stokes being considered. Defaults to "v".
        output_path (Optional[Path], optional): The path of the new catalogue. If `None` it is derived from the input `catalogue` path. Defaults to None.

    Returns:
        Path: Path to the new catalogue use for leakage
    """
    pol_fits = _load_fits_image(fits_path=pol_image)

    leakage_filters = LeakageFilters()

    components = load_and_filter_components(
        catalogue=catalogue, leakage_filters=leakage_filters
    )
    peak_flux_col = guess_column_in_table(table=components, column="peakflux")
    i_values = components[peak_flux_col]

    pol_pixel_coords = get_xy_pixel_coords(table=components, wcs=pol_fits.wcs)
    pol_stats = extract_pol_stats_in_box(
        pol_image=pol_fits.data,
        pixel_coords=pol_pixel_coords,
        search_box_size=leakage_filters.search_box_size,
        noise_box_size=leakage_filters.noise_box_size,
        mean_box_size=leakage_filters.mean_box_size,
    )
    frac_values = pol_stats.peak / i_values

    logger.info(f"{frac_values.shape=}")
    components[f"{pol}_fraction"] = frac_values
    components[f"{pol}_peak"] = pol_stats.peak
    components[f"{pol}_noise"] = pol_stats.noise
    components[f"{pol}_mean"] = pol_stats.mean

    output_path = _get_output_catalogue_path(
        input_path=catalogue if isinstance(catalogue, Path) else pol_image,
        pol=pol,
        output_path=output_path,
    )

    logger.info(f"Writing {output_path}")
    components.write(output_path, overwrite=True)

    return output_path


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Create a leakage catalogue and map")
    parser.add_argument("pol_image", type=Path, help="Path to the polarisation image")
    parser.add_argument(
        "component_catalogue", type=Path, help="Path to the component catalogue"
    )
    parser.add_argument(
        "--pol",
        default="v",
        choices=("q", "u", "v"),
        help="The polarisation that is being considered",
    )

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    create_leakge_component_table(
        pol_image=args.pol_image,
        catalogue=args.component_catalogue,
        pol=args.pol,
    )


if __name__ == "__main__":
    cli()
