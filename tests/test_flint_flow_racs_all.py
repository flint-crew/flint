"""Basic tests for utility functions in the RACS All flint flow"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import flint.prefect.flows.racs_all_continuum_selfcal as racs_all_flow
from flint.configuration import Strategy
from flint.ms import MS, find_mss
from flint.naming import CASDANameComponents, extract_components_from_name
from flint.options import RACSAllOptions
from flint.prefect.flows.racs_all_continuum_selfcal import (
    _check_create_output_science_path,
    _check_racs_all_options,
    _ensure_all_casda_format,
    get_final_mask_options,
    get_skymodel_gaincal_options,
    match_beams_across_bands,
)


def test_get_skymodel_gaincal_options_without_settings() -> None:
    """With no gaincal settings the sky-model round is a default phase-only gaincal"""
    assert get_skymodel_gaincal_options(strategy=None) == {"calmode": "p"}

    strategy = Strategy({"defaults": {}, "selfcal": {0: {"wsclean": {}}}})
    assert get_skymodel_gaincal_options(strategy=strategy) == {"calmode": "p"}


def test_get_skymodel_gaincal_options_from_strategy() -> None:
    """defaults.gaincal applies, overridden by selfcal round 0, and later rounds are ignored"""
    strategy = Strategy(
        {
            "defaults": {"gaincal": {"solint": "60s", "minsnr": 0.0}},
            "selfcal": {
                0: {"gaincal": {"solint": "30s", "uvrange": ">300m"}},
                1: {"gaincal": {"solint": "10s"}},
            },
        }
    )

    assert get_skymodel_gaincal_options(strategy=strategy) == {
        "solint": "30s",
        "minsnr": 0.0,
        "uvrange": ">300m",
        "calmode": "p",
    }


def test_get_skymodel_gaincal_options_stays_phase_only(caplog) -> None:
    """A calmode other than phase-only is ignored, with a warning"""
    strategy = Strategy(
        {
            "defaults": {"gaincal": {"calmode": "p"}},
            "selfcal": {0: {"gaincal": {"calmode": "ap", "solint": "120s"}}},
        }
    )

    with caplog.at_level(logging.WARNING):
        options = get_skymodel_gaincal_options(strategy=strategy)

    assert options == {"calmode": "p", "solint": "120s"}
    assert "Ignoring calmode='ap'" in caplog.text


@pytest.fixture
def masking_strategy() -> Strategy:
    return Strategy(
        {
            "defaults": {"masking": {"use_mac": True, "seed_clip": 4.5}},
            "selfcal": {
                round_info: {"masking": {"seed_clip": float(round_info)}}
                for round_info in range(5)
            },
        }
    )


def test_get_final_mask_options_uses_the_next_round(masking_strategy, caplog) -> None:
    """The final masks take the masking of the round after the last one imaged"""
    with caplog.at_level(logging.INFO):
        options = get_final_mask_options(strategy=masking_strategy, rounds=3)

    assert options == {"use_mac": True, "seed_clip": 4.0}
    assert "Requested the masking options of selfcal round 4" in caplog.text
    assert "falling back" not in caplog.text
    assert "Using selfcal round 4" in caplog.text


def test_get_final_mask_options_falls_back_to_the_last_round(
    masking_strategy, caplog
) -> None:
    """With no round after the last one imaged, the last round defined is used"""
    with caplog.at_level(logging.INFO):
        options = get_final_mask_options(strategy=masking_strategy, rounds=4)

    assert options == {"use_mac": True, "seed_clip": 4.0}
    assert "Requested the masking options of selfcal round 5" in caplog.text
    assert "falling back to the last round defined (4)" in caplog.text


def test_get_final_mask_options_without_selfcal_rounds() -> None:
    """Without a strategy the defaults of MaskingOptions apply, and without selfcal
    rounds the strategy's defaults do"""
    assert get_final_mask_options(strategy=None, rounds=0) == {}

    strategy = Strategy({"defaults": {"masking": {"use_mac": True}}})
    assert get_final_mask_options(strategy=strategy, rounds=0) == {"use_mac": True}


@pytest.fixture
def base_racs_all_options(tmp_path) -> RACSAllOptions:
    """A minimal RACSAllOptions with just enough set to pass the wsclean_container check"""
    wsclean_container = tmp_path / "wsclean.sif"
    wsclean_container.touch()

    return RACSAllOptions(
        low_data=tmp_path / "low",
        mid_data=tmp_path / "mid",
        high_data=tmp_path / "high",
        wsclean_container=wsclean_container,
        rounds=0,
    )


def test_check_racs_all_options_skymodel_requirements(
    base_racs_all_options, tmp_path, monkeypatch
) -> None:
    """run_skymodel_calibration requires a verified reference catalogue directory,
    and both calibrate_container and casa_container"""
    catalogues_valid: list[bool] = []
    monkeypatch.setattr(
        racs_all_flow,
        "verify_reference_catalogues",
        lambda reference_directory: catalogues_valid[-1],
    )
    _check_racs_all_options(racs_all_options=base_racs_all_options)

    container = base_racs_all_options.wsclean_container
    racs_all_options = base_racs_all_options.with_options(
        run_skymodel_calibration=True,
        calibrate_container=container,
        casa_container=container,
    )
    with pytest.raises(ValueError, match="reference_catalogue_directory"):
        _check_racs_all_options(racs_all_options=racs_all_options)

    racs_all_options = racs_all_options.with_options(
        reference_catalogue_directory=tmp_path
    )
    catalogues_valid.append(False)
    with pytest.raises(ValueError, match="reference_catalogue_directory"):
        _check_racs_all_options(racs_all_options=racs_all_options)

    catalogues_valid.append(True)
    _check_racs_all_options(racs_all_options=racs_all_options)

    for missing in ("calibrate_container", "casa_container"):
        with pytest.raises(ValueError, match=missing):
            _check_racs_all_options(
                racs_all_options=racs_all_options.with_options(**{missing: None})
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
