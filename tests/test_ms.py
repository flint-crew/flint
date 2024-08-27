"""Small tests for items related to measurement sets
and the MS class
"""

import shutil
from pathlib import Path

import numpy as np
import pytest
from casatools.table import table as table_object

from flint.calibrate.aocalibrate import ApplySolutions
from flint.exceptions import MSError
from flint.ms import (
    MS,
    check_column_in_ms,
    copy_and_preprocess_casda_askap_ms,
    find_mss,
    get_phase_dir_from_ms,
    rename_ms_and_columns_for_selfcal,
)
from flint.utils import get_packaged_resource_path

table = table_object()


def test_find_mss(tmpdir):
    tmpdir = Path(tmpdir)
    for name in range(45):
        new_ms = tmpdir / f"SB1234.Pirate_1234+456.beam{name}.ms"
        new_ms.mkdir()

        new_folder = tmpdir / f"not_and_ms_{name}.folder"
        new_folder.mkdir()

    res = find_mss(mss_parent_path=tmpdir, expected_ms_count=45)
    assert len(res) == 45

    with pytest.raises(AssertionError):
        _ = find_mss(mss_parent_path=tmpdir, expected_ms_count=49005)


@pytest.fixture
def casda_example(tmpdir):
    ms_zip = Path(
        get_packaged_resource_path(
            package="flint.data.tests",
            filename="scienceData.EMU_0529-60.SB50538.EMU_0529-60.beam08_averaged_cal.leakage.ms.zip",
        )
    )
    outpath = Path(tmpdir) / "extract"

    shutil.unpack_archive(ms_zip, outpath)

    ms_path = (
        Path(outpath)
        / "scienceData.EMU_0529-60.SB50538.EMU_0529-60.beam08_averaged_cal.leakage.ms"
    )

    return ms_path


def _test_the_data(ms):
    """Some very simple tests for the rotation. The expected numbers come from manually
    stabbing the MSs"""
    from casatools.table import table as table_object

    table = table_object()

    with table(str(ms), ack=False) as tab:
        data = tab.getcol("DATA")
        inst_data = tab.getcol("INSTRUMENT_DATA")
        colnames = tab.colnames()

    assert all([col in colnames for col in ("DATA", "INSTRUMENT_DATA")])

    expected_inst_data = np.array(
        [5.131794 - 23.130766j, 45.26275 - 45.140232j, 0.80312335 + 0.41873842j],
        dtype=np.complex64,
    )
    assert np.allclose(inst_data[:, 10, 0], expected_inst_data)

    expected_data = np.array(
        [-12.364758 - 59.172283j, -10.334289 - 97.017624j, 1.022179 - 0.18529199j],
        dtype=np.complex64,
    )
    assert np.allclose(data[:, 10, 0], expected_data)


def test_copy_preprocess_ms(casda_example, tmpdir):
    """Run the copying and preprocessing for the casda askap. This is not testing the actual contents or the
    output visibility file yet. Just sanity around the process."""

    output_path = Path(tmpdir) / "casda_ms"

    new_ms = copy_and_preprocess_casda_askap_ms(
        casda_ms=Path(casda_example), output_directory=output_path
    )
    _test_the_data(ms=new_ms.path)

    # wjem file format not recgonised
    with pytest.raises(ValueError):
        copy_and_preprocess_casda_askap_ms(
            casda_ms=Path(casda_example) / "Thisdoesnotexist",
            output_directory=output_path,
        )

    # When input directory does not exist
    with pytest.raises(AssertionError):
        copy_and_preprocess_casda_askap_ms(
            casda_ms=Path(
                "thisdoesnotexist/scienceData.EMU_0529-60.SB50538.EMU_0529-60.beam08_averaged_cal.leakage.ms"
            ),
            output_directory=output_path / "New",
        )


@pytest.fixture
def ms_example(tmpdir):
    ms_zip = Path(
        get_packaged_resource_path(
            package="flint.data.tests",
            filename="SB39400.RACS_0635-31.beam0.small.ms.zip",
        )
    )
    outpath = Path(tmpdir) / "39400"

    shutil.unpack_archive(ms_zip, outpath)

    ms_path = Path(outpath) / "SB39400.RACS_0635-31.beam0.small.ms"

    return ms_path


def test_check_column_in_ms(ms_example):
    """See whether columns are present in the MS, and whether the order of checking is correct"""
    ms = MS(path=Path(ms_example), column="DATA")

    assert check_column_in_ms(ms=ms)
    assert not check_column_in_ms(ms=ms, column="NoExists")

    with pytest.raises(ValueError):
        check_column_in_ms(ms=ms.with_options(column=None))


def _get_columns(ms_path):
    with table(str(ms_path), readonly=True, ack=False) as tab:
        return tab.colnames()


def test_rename_ms_and_columns_for_selfcal_correct2data(ms_example, tmpdir):
    """Sanity around renaming a MS and handlign the columns that should be renamed"""
    ms = MS.cast(Path(ms_example))
    with table(str(ms.path), readonly=False, ack=False) as tab:
        tab.renamecol("DATA", "CORRECTED_DATA")

    colnames = _get_columns(ms_path=ms.path)

    assert "DATA" not in colnames
    assert "CORRECTED_DATA" in colnames

    target = Path(tmpdir) / "target_directory"
    target.mkdir(parents=True)
    target = target / ms.path.name

    new_ms = rename_ms_and_columns_for_selfcal(ms=ms, target=target)
    new_colnames = _get_columns(ms_path=new_ms.path)

    assert new_ms.path == target
    assert "DATA" in new_colnames
    assert "CORRECTED_DATA" not in new_colnames


def test_rename_ms_and_columns_for_selfcal(ms_example, tmpdir):
    """Sanity around renaming a MS and handlign the columns that should be renamed"""
    ms = MS.cast(Path(ms_example))
    colnames = _get_columns(ms_path=ms.path)

    assert "DATA" in colnames

    target = Path(tmpdir) / "target_directory"
    target.mkdir(parents=True)
    target = target / ms.path.name

    new_ms = rename_ms_and_columns_for_selfcal(ms=ms, target=target)
    new_colnames = _get_columns(ms_path=new_ms.path)

    assert new_ms.path == target
    assert "DATA" in new_colnames


def test_phase_dir(ms_example):
    pos = get_phase_dir_from_ms(ms=ms_example)

    assert np.isclose(pos.ra.deg, 98.211959)
    assert np.isclose(pos.dec.deg, -30.86099889)


def test_ms_self_attribute():
    ex = Path("example/jack_sparrow.ms")
    ms = MS(path=ex)

    assert ms.ms.path == ex


def test_ms_from_options():
    path = Path("example.ms")
    solutions = ApplySolutions(
        cmd="none", solution_path=Path("example_sols.bin"), ms=MS(path=path)
    )

    example_ms = MS.cast(solutions)
    ms = MS(path=path)

    assert isinstance(example_ms, MS)
    assert example_ms == ms


def test_raise_error_ms_from_options():
    path = Path("example.ms")
    solutions = ApplySolutions(
        cmd="none", solution_path=Path("example_sols.bin"), ms=path
    )

    with pytest.raises(MSError):
        MS.cast(solutions)
