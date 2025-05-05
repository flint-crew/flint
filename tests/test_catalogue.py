"""Tests that work around the catalogue functionality"""

from __future__ import annotations

from pathlib import Path

import pytest
from astropy.table import Table

from flint.catalogue import (
    KNOWN_REFERENCE_CATALOGUES,
    Catalogue,
    _guess_catalogue_type,
    download_reference_catalogues,
    download_vizier_catalogue,
    get_reference_catalogue,
    guess_column_in_table,
    verify_reference_catalogues,
)
from flint.utils import get_packaged_resource_path


def test_verify_catalogue_check(tmpdir):
    """Makes sure that the verify catalogue function works nicely.
    Only a basic check if it exists is performed, don't try to
    download"""

    catalogue_directory = Path(tmpdir) / "catalogue1"
    catalogue_directory.mkdir(parents=True)

    assert not verify_reference_catalogues(
        reference_directory=catalogue_directory, load_catalogue=False
    )

    for k, v in KNOWN_REFERENCE_CATALOGUES.items():
        file_name = v.file_name
        temp = catalogue_directory / file_name
        temp.touch()

    assert verify_reference_catalogues(
        reference_directory=catalogue_directory, load_catalogue=False
    )


def _get_aegean_catalogue():
    table_path = get_packaged_resource_path(
        package="flint.data.tests",
        filename="SB38959.RACS_1357-18.noselfcal.linmos_comp.fits",
    )
    table = Table.read(table_path)
    return table, table_path


def test_guess_column_names():
    """See if the correct column names can be guessed from a table"""
    table, _ = _get_aegean_catalogue()

    exp = dict(
        ra="ra",
        dec="dec",
        intflux="int_flux",
        peakflux="peak_flux",
        intfluxerr="local_rms",
    )
    for key, value in exp.items():
        assert value == guess_column_in_table(table=table, column=key)
        assert value == guess_column_in_table(
            table=table, column=key, guess_column="JackSparrow"
        )

    with pytest.raises(KeyError):
        guess_column_in_table(table=table, column="JackSparrow")


def test_catalogue_type():
    """Testing the guessing of a catalogue. At the moment this
    guess function is a stub function for the moment."""
    table, table_path = _get_aegean_catalogue()

    cata = _guess_catalogue_type(table=table_path)
    assert isinstance(cata, Catalogue)

    cata2 = _guess_catalogue_type(table=table)
    assert isinstance(cata2, Catalogue)


def test_known_reference_catalogues():
    """Make sure all of the known reference catalogues have a vizier id attached"""
    assert all([cata.vizier_id for cata in KNOWN_REFERENCE_CATALOGUES.values()])


def test_no_reference_catalogue():
    """Ensure file not found error raised if catalogue not found"""
    with pytest.raises(FileNotFoundError):
        _ = get_reference_catalogue(reference_directory=Path("./"), survey="NVSS")


@pytest.mark.flaky(reruns=3, reruns_delay=2)
def test_download_vizier_catalogue(tmpdir):
    """Download a example vizier table"""

    output_path = Path(tmpdir) / "catalogue1/ICRF.fits"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    icrf_id = KNOWN_REFERENCE_CATALOGUES["ICRF"]
    assert icrf_id.vizier_id
    cata_path = download_vizier_catalogue(
        output_path=output_path, vizier_id=icrf_id.vizier_id
    )

    assert cata_path == output_path
    assert cata_path.exists()

    table = Table.read(cata_path)
    assert len(table) == 3414


@pytest.mark.flaky(reruns=3, reruns_delay=2)
def test_get_vizier_catalogue(tmpdir):
    """Download a example vizier table"""
    output_path = Path(tmpdir) / "catalogue1/ICRF.fits"
    assert not output_path.exists()

    output_path = Path(tmpdir) / "catalogue1/ICRF.fits"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    icrf = KNOWN_REFERENCE_CATALOGUES["ICRF"]
    assert icrf.vizier_id
    _ = download_vizier_catalogue(output_path=output_path, vizier_id=icrf.vizier_id)
    assert output_path.exists()

    table, catalogue = get_reference_catalogue(
        reference_directory=output_path.parent, survey="ICRF"
    )
    assert catalogue.file_name == "ICRF.fits"
    assert catalogue.survey == "ICRF"
    assert len(table) == 3414

    with pytest.raises(ValueError):
        _, _ = get_reference_catalogue(
            reference_directory=output_path.parent, survey="Jack"
        )


@pytest.mark.flaky(reruns=3, reruns_delay=2)
def test_download_vizier_catalogue_dryrun(tmpdir):
    """See if the dry run option in download a example vizier table"""

    output_path = Path(tmpdir) / "cataloguedry/ICRF.fits"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    icrf_cata = KNOWN_REFERENCE_CATALOGUES["ICRF"]
    assert icrf_cata.vizier_id is not None

    cata_path = download_vizier_catalogue(
        output_path=output_path, vizier_id=icrf_cata.vizier_id, dry_run=True
    )

    assert cata_path == output_path
    assert not cata_path.exists()


@pytest.mark.flaky(reruns=3, reruns_delay=2)
def test_download_reference_catalogues(tmpdir):
    """Ensure all catalogues can be downloaded. Not the dry_run=True,
    meaning the catalogues are not all actually dowenloaded
    """
    output_dir = Path(tmpdir) / "catalogue2"
    output_dir.mkdir(parents=True, exist_ok=True)

    outputs = download_reference_catalogues(
        reference_directory=output_dir, dry_run=True
    )

    assert len(outputs) == len(KNOWN_REFERENCE_CATALOGUES)
