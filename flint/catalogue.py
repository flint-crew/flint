"""Utilities around catalogues.

Known reference catalogues are described by their ViZeR catalogue id,
which are used to download and store the appropriately formed catalogues on disk.
If the ViZeR service is down then attempts to download and form FITS catalgoues
will fail. These only need to be downloaded once, provided they can be stored
and retained on disk.
"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import NamedTuple

import astropy.units as u
from astropy.table import Table
from astroquery.vizier import Vizier

from flint.logging import logger


class Catalogue(NamedTuple):
    """A basic structure used to describe a known catalogue."""

    survey: str
    """Shorthand name of the sourcey catalogue"""
    file_name: str
    """The file name of the known catalogue"""
    freq: float  # Hertz
    """Reference frequency of the catalogue, in Hertz"""
    ra_col: str
    """Column name containing the right-ascension"""
    dec_col: str
    """Column name containing the declination"""
    name_col: str
    """Column name containing the source/component name"""
    flux_col: str
    """Column name containing the flux density"""
    maj_col: str
    """Column name containing the major-axis of the source gaussian component"""
    min_col: str
    """Column name containing the min-axis of the source gaussian component"""
    pa_col: str
    """Column name containing the pa of the source gaussian component"""
    alpha_col: str | None = None  # Used to scale the SED
    """Column name containing the spectral index, used to calculate the source SED. If None a default is used. """
    q_col: str | None = None  # Used to scale the SED
    """Column name containing the curvature of the spectral index, used to calculate the source SED. If None a default is used. """
    vizier_id: str | None = (
        None  # Required for known reference catalogues, not for other specified catalogues
    )
    """The ID of the catalogue on Vizier that is used to download the catalogue"""


KNOWN_REFERENCE_CATALOGUES: dict[str, Catalogue] = dict(
    NVSS=Catalogue(
        survey="NVSS",
        file_name="NVSS.fits",
        name_col="NVSS",
        freq=1.4e9,
        ra_col="_RAJ2000",
        dec_col="_DEJ2000",
        flux_col="S1.4",
        maj_col="MajAxis",
        min_col="MinAxis",
        pa_col="PA",
        vizier_id="VIII/65/nvss",
    ),
    SUMSS=Catalogue(
        survey="SUMSS",
        file_name="SUMSS.fits",
        freq=8.43e8,
        ra_col="_RAJ2000",
        dec_col="_DEJ2000",
        name_col="Mosaic",
        flux_col="St",
        maj_col="dMajAxis",
        min_col="dMinAxis",
        pa_col="dPA",
        vizier_id="VIII/81B/sumss212",
    ),
    ICRF=Catalogue(
        survey="ICRF",
        file_name="ICRF.fits",
        freq=1e9,
        ra_col="_RAJ2000",
        dec_col="_DEJ2000",
        name_col="ICRF",
        flux_col="None",
        maj_col="None",
        min_col="None",
        pa_col="None",
        vizier_id="I/323/icrf2",
    ),
    RACSLOW=Catalogue(
        file_name="racs-low.fits",
        survey="RACS-LOW",
        freq=887.56e6,
        ra_col="_RAJ2000",
        dec_col="_DEJ2000",
        name_col="GID",
        flux_col="Ftot",
        maj_col="amaj",
        min_col="bmin",
        pa_col="PA",
        vizier_id="J/other/PASA/38.58/gausscut",
    ),
)

# Helper functions that are used to try to guess column names from a table.
# These are intended to only be helpers for common names and not an extensive list
PREFERRED_RA_COLUMN_NAMES = ["RAJ2000", "ra"]
PREFERRED_DEC_COLUMN_NAMES = ["DEJ2000", "dec"]
PREFERRED_PEAK_COLUMN_NAMES = ["peak_flux", "Sp"]
PREFERRED_INT_COLUMN_NAMES = ["int_flux", "Sint"]
PREFERRED_INT_ERR_COLUMN_NAMES = ["local_rms"]

PREFERRED_COLUMNS = dict(
    ra=PREFERRED_RA_COLUMN_NAMES,
    dec=PREFERRED_DEC_COLUMN_NAMES,
    peakflux=PREFERRED_PEAK_COLUMN_NAMES,
    intflux=PREFERRED_INT_COLUMN_NAMES,
    intfluxerr=PREFERRED_INT_ERR_COLUMN_NAMES,
)


def guess_column_in_table(
    table: Table, column: str, guess_column: str | None = None
) -> str:
    """Attempt to deduce the appropriate column name from a set of
    column names in a table. A lookup of known column names for different
    contexts if consulted. Available modes are:

    #. ra
    #. dec
    #. peakflux
    #. intflux

    If `guess_column` is provided and is in the table this is returned

    Args:
        table (Table): The table with the column names to inspect
        column (str): The type of the column we are attempting to deduce.
        guess_column (Optional[str], optional): Consider whether this column exists first. Defaults to None.

    Raises:
        ValueError: Raised when either the RA or Dec columns could not be figured out

    Returns:
        str: The names of the peak flux column
    """

    logger.debug(f"Guessing column name for {column=} with {guess_column=}")
    column_names = [col.upper() for col in table.colnames]
    preferred_columns = PREFERRED_COLUMNS.get(column, None)
    if preferred_columns is None:
        raise KeyError(f"{column=} not in {PREFERRED_COLUMNS.keys()}")

    cols = (
        [guess_column]
        if guess_column and guess_column.upper() in column_names
        else [col for col in preferred_columns if col.upper() in column_names]
    )

    if not len(cols) > 0:
        raise ValueError(
            f"Unable to guess {column=} column names. Table has {column_names=}, and {preferred_columns=}"
        )

    return cols[0]


def _guess_catalogue_type(
    table: Table | Path,
    survey: str = "askap",
    file_name: str = "askap.fit",
    freq: float = -1,
) -> Catalogue:
    """This is a stub function that will be expanded in the future. It
    is intended to guess the appropriate source finder that constructed
    a component catalogue, resolving issues around the column names.

    This stub function will return a aegean catalogue description

    Args:
        table (Union[Table, Path]): The table that will be inspected
        survey (str, optional): The name of the survey to use. Defaults to askap.
        file_name (str, optional): The file name that would be used for the catalogue. Defaults of askap.fits.

    Returns:
        Catalogue: Placeholder Catalogue representing Aegean catalogues
    """
    table = Table.read(table) if isinstance(table, Path) else table

    return Catalogue(
        survey=survey,
        file_name=file_name,
        freq=freq,
        name_col="source",
        ra_col="ra",
        dec_col="dec",
        flux_col="int_flux",
        maj_col="a",
        min_col="b",
        pa_col="pa",
    )


def get_reference_catalogue(
    reference_directory: Path, survey: str, verify: bool = True
) -> tuple[Table, Catalogue]:
    """Load in a known reference catalogue

    If `verify` is `True` then the `Catalogue.flux_col`, if present, will have
    its units converted to Jy.

    Args:
        reference_directory (Path): The path to the directory where reference catalogues were downloaded to
        survey (str): The name of the survey to load.
        verify (bool, optional): If `True`, the table column names are inspected to ensure they are correct. Defaults to True.

    Raises:
        ValueError: Raised when the requested survey is not known

    Returns:
        Tuple[Table, Catalogue]: The loaded table and corresponding set of expected columns
    """

    catalogue = KNOWN_REFERENCE_CATALOGUES.get(survey, None)

    if catalogue is None:
        raise ValueError(
            f"{survey=} is not known, recognized reference catalogues are {KNOWN_REFERENCE_CATALOGUES.keys()}"
        )

    table_path = reference_directory / catalogue.file_name
    logger.info(f"Loading {table_path=}")

    if not table_path.exists():
        raise FileNotFoundError(
            f"{table_path=} not found. Check {reference_directory=} for known catalogues"
        )

    table = Table.read(table_path)

    if verify:
        valid_cols = [
            col in table.columns
            for col in (
                catalogue.ra_col,
                catalogue.dec_col,
                catalogue.name_col,
                catalogue.flux_col,
                catalogue.maj_col,
                catalogue.min_col,
            )
            if col.lower() != "none"
        ]
        assert all(valid_cols), f"Column is not valid, {valid_cols=}"
        if catalogue.flux_col.lower() != "none":
            assert isinstance(table[catalogue.flux_col].unit, u.Unit)
            table[catalogue.flux_col] = table[catalogue.flux_col].to(u.Jy)

    return table, catalogue


def download_vizier_catalogue(
    output_path: Path, vizier_id: str, dry_run: bool = False
) -> Path:
    """Download a catalogue from the vizier catalogue service. The table
    will be obtained using astroquery and written out to the supplied
    `output_path`, from which the format is inferred.

    Args:
        output_path (Path): Where the table will be written to
        vizier_id (str): The catalogue ID that will be downloaded
        dry_run (bool, optional): If `True`, no downloading will take place. Defaults to False.

    Returns:
        Path: Path the file was written to
    """
    logger.info(f"Downloading {vizier_id=}")

    if dry_run:
        logger.info(f"{dry_run=}, not downloading")
        return output_path

    tablelist = Vizier(
        columns=["_RAJ2000", "_DEJ2000", "all"], row_limit=-1
    ).get_catalogs(vizier_id, verbose=True)
    logger.info(f"catalogue downloaded, contains {len(tablelist[0])} rows")
    logger.info(f"Writing {vizier_id=} to {output_path=}")

    assert len(tablelist) == 1, (
        f"Table list for {vizier_id=} has unexpected length of {len(tablelist)}"
    )

    # Note all pirates respect the FITS standard@
    if description := tablelist[0].meta.get("description", None):
        tablelist[0].meta["description"] = description[:30]

    tablelist[0].write(output_path, overwrite=True)

    return output_path


def download_reference_catalogues(
    reference_directory: Path, dry_run: bool = False
) -> tuple[Path, ...]:
    """Download all of the expected reference catalogue data that flint relies on

    Args:
        reference_directory (Path): The parent directory catalgoues will be written to
        dry_run (bool, optional): If `True`, no downloading will take place. Defaults to False.

    Returns:
        Tuple[Path, ...]: Collection of paths of all the downloaded reference catalogues
    """

    logger.info(f"Downloading catalogues to {reference_directory=}")
    reference_directory.mkdir(parents=True, exist_ok=True)

    logger.info(f"Downloading {len(KNOWN_REFERENCE_CATALOGUES)} catalogues")
    catalogue_paths = [
        download_vizier_catalogue(
            output_path=(reference_directory / f"{catalogue.file_name}").absolute(),
            vizier_id=catalogue.vizier_id,
            dry_run=dry_run,
        )
        for _, catalogue in KNOWN_REFERENCE_CATALOGUES.items()
        if catalogue.vizier_id
    ]

    return tuple(catalogue_paths)


def list_known_reference_catalogues() -> None:
    """List the known reference catalogues that are expected/downloaded by flint"""

    logger.info(f"{len(KNOWN_REFERENCE_CATALOGUES)} are known")
    for survey, cata in KNOWN_REFERENCE_CATALOGUES.items():
        logger.info(f"{survey=}")
        logger.info(f"{Catalogue}")


def verify_reference_catalogues(
    reference_directory: Path, load_catalogue: bool = True
) -> bool:
    """Attempt to load the set of reference catalogues to ensure they are correctly
    formed

    Args:
        reference_directory (Path): The directory containing the reference catalogues
        load_catalogue (bool, optional): Load the catalogue as part of verification. If False only check to see if the file exists. Defaults to True.

    Returns:
        bool: Indicates whether all catalogue files exist and are correctly formed
    """

    logger.info(f"Verifying catalogues in {reference_directory=}")
    logger.info(f"Searching for {len(KNOWN_REFERENCE_CATALOGUES)}")
    survey_valid = {}
    for survey, cata in KNOWN_REFERENCE_CATALOGUES.items():
        try:
            if load_catalogue:
                _ = get_reference_catalogue(
                    reference_directory=reference_directory, survey=survey
                )
            else:
                cata_path = reference_directory / cata.file_name
                assert cata_path.exists()
            valid = True
        except (ValueError, AssertionError, FileNotFoundError):
            valid = False
        logger.info(f"{survey=} is {'valid' if valid else 'not valid'}")
        survey_valid[survey] = valid

    return all(survey_valid.values())


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Utilities around catalogues")

    subparser = parser.add_subparsers(
        dest="mode", help="Operation mode of flint_catalogue"
    )

    download_parser = subparser.add_parser(
        "download", help="Download reference catalogues"
    )
    download_parser.add_argument(
        "reference_directory",
        type=Path,
        help="The directory to save the reference catalogues to",
    )

    _ = subparser.add_parser("list", help="List the known reference catalogues")

    verify_parser = subparser.add_parser(
        "verify",
        help="Ensure the expected catalogues existing in the reference directory and are correctly formed",
    )
    verify_parser.add_argument(
        "reference_directory",
        type=Path,
        help="Directory containing the known reference catalogues",
    )

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    if args.mode == "download":
        download_reference_catalogues(reference_directory=args.reference_directory)
    elif args.mode == "list":
        list_known_reference_catalogues()
    elif args.mode == "verify":
        verify_reference_catalogues(reference_directory=args.reference_directory)
    else:
        logger.info(f"Mode {args.mode} is not recognised")
        parser.print_help()


if __name__ == "__main__":
    cli()
