"""Tests for the sun tractor suppression"""

from __future__ import annotations

from pathlib import Path

import astropy.units as u
import pytest
from jolly_roger.tractor import TukeyTractorOptions as JollyTukeyTaperOptions

from flint.options import MS
from flint.peel.jolly import (
    TukeyTractorOptions,
    _create_jolly_tractor_options,
    jolly_roger_tractor,
)


def test_raise_bad_tukey_options() -> None:
    """Ensure that the type converter is raising an error
    on an incorrect type"""

    ms_path = Path("JackBeHere.ms")
    with pytest.raises(TypeError):
        _create_jolly_tractor_options(
            ms_path=ms_path,
            tukey_tractor_options=1.0,  # type: ignore
        )


def test_convert_tractor() -> None:
    """Make sure that we are converting the flint tractor options class
    to the jolly-roget tractor class. This is needed to avoid serialising
    astropy units."""

    ms_path = Path("JackSparrow.ms")

    tukey_tractor_options = TukeyTractorOptions()
    jolly_tractor_options = _create_jolly_tractor_options(
        ms_path=ms_path, tukey_tractor_options=tukey_tractor_options
    )
    assert isinstance(jolly_tractor_options, JollyTukeyTaperOptions)
    assert jolly_tractor_options.ms_path == ms_path
    assert (
        jolly_tractor_options.elevation_cut
        == tukey_tractor_options.elevation_cut_deg * u.deg
    )


def test_tukey_tractor(ms_example) -> None:
    """Run the tukey tractor to assess for simple errors"""

    tukey_tractor_options = TukeyTractorOptions(apply_towards_object=True)
    ms_path = Path(ms_example)

    ms = jolly_roger_tractor(ms=ms_path, tukey_tractor_options=tukey_tractor_options)
    assert isinstance(ms, MS)
