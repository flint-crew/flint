"""Tests around the crystalball prediction"""

from __future__ import annotations

from pathlib import Path

import pytest
from casacore.tables import table

from flint.ms import MS
from flint.predict.crystalball import (
    CrystalBallOptions,
    crystalball_predict,
    get_parser,
)
from flint.utils import get_packaged_resource_path


def test_get_parser():
    """A simple test to obtain the parser. Tests imports as well."""
    _ = get_parser()


def test_expect_asserts():
    """The crystallbal predict will raise some errors should things not be found"""

    # This one is testing to make sure we catch cases where guessing of the
    # wsclean source list name points to something that doesn't exist
    crystalball_options = CrystalBallOptions()
    ms = MS(path=Path("JackNotHere.ms"))
    with pytest.raises(FileNotFoundError):
        crystalball_predict(ms=ms, crystalball_options=crystalball_options)

    # Tess to make sure we error out on a single pol
    crystalball_options = CrystalBallOptions(
        crystallball_wsclean_pol_mode=["i", "q", "u", "v"]
    )
    ms = MS(path=Path("JackNotHere.ms"))
    with pytest.raises(AssertionError):
        crystalball_predict(ms=ms, crystalball_options=crystalball_options)


def test_example_prediction_ms(ms_example_with_name):
    """Attempt to predict into the MS with crystalball"""
    ms_path = ms_example_with_name("Jack")
    assert ms_path.exists()
    ms = MS(path=ms_path)
    assert ms.model_column is None

    model_path = get_packaged_resource_path(
        package="flint.data.models", filename="1934-638.calibrate.txt"
    )
    assert model_path.exists()

    with table(str(ms_path)) as tab:
        assert "MODEL_DATA" not in tab.colnames()

    crystalball_options = CrystalBallOptions()
    ms = crystalball_predict(
        ms=ms,
        crystalball_options=crystalball_options,
        wsclean_source_list_path=model_path,
    )
    assert ms.model_column == "MODEL_DATA"

    with table(str(ms_path)) as tab:
        assert "MODEL_DATA" in tab.colnames()

    # TODO: Need to ensure that the 1934 predict model data is actually correct
