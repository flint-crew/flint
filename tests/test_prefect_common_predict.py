"""Tests around the prefect tasks for model visibility prediction"""

from __future__ import annotations

from pathlib import Path

import pytest

import flint.prefect.common.predict as predict_module
from flint.exceptions import NoFieldSourceError
from flint.ms import MS
from flint.prefect.common.predict import task_create_sky_model
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
