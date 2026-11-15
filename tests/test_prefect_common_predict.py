"""Tests around the prefect tasks for model visibility prediction"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

import flint.prefect.common.predict as predict_module
from flint.configuration import FORMAT_VERSION, Strategy
from flint.exceptions import NoFieldSourceError
from flint.ms import MS
from flint.options import FieldOptions
from flint.prefect.common.predict import (
    calibrate_mss_against_sky_model,
    check_skymodel_calibration_options,
    get_skymodel_dp3_options,
    get_skymodel_gaincal_options,
    task_create_sky_model,
)
from flint.sky_model import SkyModelOptions


def test_task_create_sky_model_no_sources_raises(monkeypatch) -> None:
    """A field with no sources left to model stops with a named error"""
    monkeypatch.setattr(
        predict_module,
        "create_sky_model",
        lambda ms_path, sky_model_options, holofile: None,
    )

    with pytest.raises(NoFieldSourceError, match="fwhm_scale_cutoff"):
        task_create_sky_model.fn(
            ms=MS(path=Path("field.beam00.ms")),
            sky_model_options=SkyModelOptions(),
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


def test_check_skymodel_calibration_options_holofiles(tmp_path) -> None:
    """Unset holography cubes are allowed, but a set one has to exist"""
    holofile = tmp_path / "holo.fits"
    holofile.touch()
    field_options = FieldOptions(
        reference_catalogue_directory=tmp_path,
        calibrate_container=holofile,
        casa_container=holofile,
    )
    check_skymodel_calibration_options(
        field_options=field_options, holofiles=[None, holofile]
    )

    with pytest.raises(ValueError, match="missing.fits"):
        check_skymodel_calibration_options(
            field_options=field_options,
            holofiles=[holofile, None, tmp_path / "missing.fits"],
        )


def test_get_skymodel_dp3_options_stays_scalarphase(caplog) -> None:
    """DP3 gaincal options come from round 0, always solving for scalarphase"""
    assert get_skymodel_dp3_options(strategy=None) == {"caltype": "scalarphase"}

    strategy = Strategy(
        {
            "defaults": {"dp3gaincal": {"solint": 6}},
            "selfcal": {0: {"dp3gaincal": {"caltype": "diagonal", "nchan": 1}}},
        }
    )
    with caplog.at_level(logging.WARNING):
        options = get_skymodel_dp3_options(strategy=strategy)

    assert options == {"solint": 6, "nchan": 1, "caltype": "scalarphase"}
    assert "Ignoring caltype='diagonal'" in caplog.text


def write_strategy(path: Path, round_0: dict) -> Path:
    """Write a strategy whose round 0 is ``round_0``."""
    path.write_text(
        yaml.dump({"version": FORMAT_VERSION, "defaults": {}, "selfcal": {0: round_0}})
    )
    return path


def test_check_skymodel_calibration_options_solver(tmp_path) -> None:
    """CASA is needed only when round 0 uses gaincal, and dp3ddecal is refused"""
    field_options = FieldOptions(
        reference_catalogue_directory=tmp_path,
        calibrate_container=tmp_path / "calibrate.sif",
        imaging_strategy=write_strategy(
            tmp_path / "dp3.yaml", round_0={"dp3gaincal": {}}
        ),
    )
    check_skymodel_calibration_options(field_options=field_options, holofiles=[None])

    cases: list[tuple[dict, str]] = [
        ({"dp3ddecal": {}}, "dp3gaincal, not dp3ddecal"),
        ({"gaincal": {}}, "casa_container"),
    ]
    for round_0, match in cases:
        imaging_strategy = write_strategy(tmp_path / "other.yaml", round_0=round_0)
        with pytest.raises(ValueError, match=match):
            check_skymodel_calibration_options(
                field_options=field_options.with_options(
                    imaging_strategy=imaging_strategy
                ),
                holofiles=[None],
            )


class Recorder:
    """Stands in for a task, keeping the arguments it is mapped with"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def map(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs.get("ms")


@pytest.mark.parametrize(
    "round_0, solver", [({"gaincal": {}}, "gaincal"), ({"dp3gaincal": {}}, "dp3")]
)
def test_calibrate_mss_against_sky_model_solver(
    tmp_path, monkeypatch, round_0, solver
) -> None:
    """Round 0 of the strategy picks CASA gaincal or DP3 gaincal for the sky-model round"""
    tasks = {"gaincal": Recorder(), "dp3": Recorder()}
    monkeypatch.setattr(predict_module, "task_create_sky_model", Recorder())
    monkeypatch.setattr(predict_module, "task_addmodel_to_ms", Recorder())
    monkeypatch.setattr(predict_module, "task_gaincal_applycal_ms", tasks["gaincal"])
    monkeypatch.setattr(predict_module, "task_dp3_selfcal_ms", tasks["dp3"])

    strategy = Strategy({"defaults": {}, "selfcal": {0: round_0}})
    calibrate_mss_against_sky_model(
        mss=[MS(path=tmp_path / "field.beam00.ms")],
        holofiles=[None],
        strategy=strategy,
        field_options=FieldOptions(reference_catalogue_directory=tmp_path),
    )

    (call,) = tasks[solver].calls
    assert call["selfcal_round"] == 0
    assert not tasks["gaincal" if solver == "dp3" else "dp3"].calls
    if solver == "dp3":
        assert call["mode"] == "dp3gaincal"
        assert call["update_dp3_options"].value == {"caltype": "scalarphase"}
