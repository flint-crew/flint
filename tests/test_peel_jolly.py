"""Tests for the sun tractor suppression"""

from __future__ import annotations

from pathlib import Path

import astropy.units as u
from jolly_roger.tractor import TukeyTractorOptions as JollyTukeyTaperOptions

from flint.peel.jolly import TukeyTractorOptions, _create_jolly_tractor_options


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
