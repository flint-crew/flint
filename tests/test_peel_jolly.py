"""Tests for the sun tractor suppression"""

from __future__ import annotations

from pathlib import Path

import astropy.units as u
import pytest
from jolly_roger.tractor import TukeyTractorOptions

from flint.options import MS
from flint.peel.jolly import (
    jolly_roger_tractor,
)


def test_raise_bad_tukey_options() -> None:
    """Ensure that the type converter is raising an error
    on an incorrect type"""

    ms_path = Path("JackBeHere.ms")
    with pytest.raises(TypeError):
        TukeyTractorOptions(
            ms_path=ms_path,
            elevation_cut_deg=1.0 * u.deg,  # type: ignore
        )


def test_tukey_tractor(ms_example) -> None:
    """Run the tukey tractor to assess for simple errors"""

    ms_path = Path(ms_example)

    tukey_tractor_options = TukeyTractorOptions(ms_path=ms_path, target_objects=["sun"])

    ms = jolly_roger_tractor(ms=ms_path, tukey_tractor_options=tukey_tractor_options)
    assert isinstance(ms, MS)
