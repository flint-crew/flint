"""Tests around the prefect tasks for model visibility prediction"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import flint.prefect.common.predict as predict_module
from flint.configuration import Strategy
from flint.exceptions import NoFieldSourceError
from flint.ms import MS
from flint.prefect.common.predict import (
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
