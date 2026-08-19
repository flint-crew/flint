"""Basic tests for utility functions in the RACS All flint flow"""

from __future__ import annotations

from pathlib import Path

import pytest

from flint.ms import MS, find_mss
from flint.naming import CASDANameComponents, extract_components_from_name
from flint.prefect.flows.racs_all_continuum_selfcal import (
    _check_create_output_science_path,
    _ensure_all_casda_format,
    match_beams_across_bands,
)


def test_check_create_output_science_path(tmpdir, monkeypatch) -> None:
    """Ensure we can establish the output science path for the racs all processing"""

    new_cwd = Path(tmpdir) / "BlackPearl"
    new_cwd.mkdir(parents=True)

    monkeypatch.chdir(new_cwd)

    test_raw_data = Path("Jack/Sparrow/23456/")
    cwd = Path.cwd()

    output_path = _check_create_output_science_path(
        science_path=test_raw_data, output_path=None, check_exists=True
    )
    assert output_path == Path(cwd) / "23456"

    # This should raise an error on second attempt
    with pytest.raises(ValueError):
        _check_create_output_science_path(
            science_path=test_raw_data, output_path=None, check_exists=True
        )

    outdir = Path(tmpdir)
    output_path = _check_create_output_science_path(
        science_path=test_raw_data, output_path=outdir, check_exists=True
    )
    assert output_path == outdir / "23456"

    with pytest.raises(ValueError):
        _check_create_output_science_path(
            science_path=test_raw_data, output_path=outdir, check_exists=True
        )


def test_find_mss_casda(tmpdir) -> None:
    """Make sure we can find the MSs. This is a sanity check to make sure the
    beams can be extracted from the yet to be processed name"""
    tmp_dir = Path(tmpdir) / "findmss_low"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    bookkeeping: dict[Path, int] = {}
    for beam in range(36):
        beam_path = (
            tmp_dir
            / f"scienceData.RACS_1041+18.SB56289.RACS_1041+18.beam{beam:02d}_averaged_cal.leakage.ms"
        )
        beam_path.mkdir(parents=True)
        assert beam_path.exists()
        bookkeeping[beam_path] = beam

    beams = find_mss(mss_parent_path=tmp_dir, expected_ms_count=36)

    for beam in beams:
        component = extract_components_from_name(name=beam.path)
        assert isinstance(component, CASDANameComponents)
        assert bookkeeping[beam.path] == int(component.beam)


@pytest.fixture
def mock_band_mss(tmpdir) -> tuple[Path, Path, Path]:
    """Create mock beams"""
    low_band = Path(tmpdir) / "56289"
    for beam in range(36):
        beam_path = (
            low_band
            / f"scienceData.RACS_1041+18.SB56289.RACS_1041+18.beam{beam:02d}_averaged_cal.leakage.ms"
        )
        beam_path.mkdir(parents=True, exist_ok=True)

    mid_band = Path(tmpdir) / "67042"
    for beam in range(36):
        beam_path = (
            mid_band
            / f"scienceData.RACS_1041+18.SB67042.RACS_1041+18.beam{beam:02d}_averaged_cal.leakage.ms"
        )
        beam_path.mkdir(parents=True, exist_ok=True)

    high_band = Path(tmpdir) / "35212"
    for beam in range(36):
        beam_path = (
            high_band
            / f"scienceData.RACS_1041+18.SB35212.RACS_1041+18.beam{beam:02d}_averaged_cal.leakage.ms"
        )
        beam_path.mkdir(parents=True, exist_ok=True)

    return low_band, mid_band, high_band


def test_match_beams_across_bands(mock_band_mss) -> None:
    """Make up beams across bands and the match them together"""
    low_band, mid_band, high_band = mock_band_mss

    sorted_results = match_beams_across_bands(
        low_mss=find_mss(mss_parent_path=low_band, expected_ms_count=36),
        mid_mss=find_mss(mss_parent_path=mid_band, expected_ms_count=36),
        high_mss=find_mss(mss_parent_path=high_band, expected_ms_count=36),
    )

    assert len(sorted_results) == 36
    for beam_idx, beam_mss in enumerate(sorted_results):
        assert len(beam_mss) == 3
        for ms in beam_mss:
            assert f"beam{beam_idx:02d}" in str(ms.path.name)


def test_check_all_casda_format(mock_band_mss) -> None:
    low_band, mid_band, high_band = mock_band_mss

    sorted_results = match_beams_across_bands(
        low_mss=find_mss(mss_parent_path=low_band, expected_ms_count=36),
        mid_mss=find_mss(mss_parent_path=mid_band, expected_ms_count=36),
        high_mss=find_mss(mss_parent_path=high_band, expected_ms_count=36),
    )
    _ensure_all_casda_format(mss_by_beams=sorted_results)

    _temp = list(sorted_results[0])
    _temp.append(MS(path=Path("AnExampleThing.ms")))

    sorted_results_bad = tuple([tuple(_temp), *sorted_results[1:]])

    with pytest.raises(ValueError):
        _ensure_all_casda_format(mss_by_beams=sorted_results_bad)
