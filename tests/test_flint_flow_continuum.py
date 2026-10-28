"""Tests around the continuum flint flow"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from prefect import task
from prefect.cache_policies import NO_CACHE
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness

import flint.prefect.common.predict as predict_module
import flint.prefect.flows.continuum_pipeline as continuum_flow
from flint.ms import MS
from flint.naming import extract_components_from_name, get_selfcal_ms_name
from flint.options import FieldOptions
from flint.prefect.flows.continuum_pipeline import (
    _check_field_options,
    process_science_fields,
)


@dataclass
class FakeSummary:
    ms: MS


@dataclass
class FakeResult:
    ms: MS


class SubmitRecorder:
    """Stands in for a task, noting the order the flow submits it in"""

    def __init__(self, name: str, task_: Any, submitted: list[str]) -> None:
        self.name = name
        self.task = task_
        self.submitted = submitted

    def map(self, **kwargs: Any) -> Any:
        self.submitted.append(self.name)
        return self.task.map(**kwargs)

    def submit(self, **kwargs: Any) -> Any:
        self.submitted.append(self.name)
        return self.task.submit(**kwargs)


def test_process_science_fields_dataflow(tmp_path, monkeypatch) -> None:
    """Each beam is carried through every round on its own, and no convolution or
    linmos is submitted before the last imaging and self-calibration job"""
    beams = (0, 1)
    calls: dict[str, list[dict[str, Any]]] = {}
    submitted: list[str] = []

    def fake(name: str, returns: Any = None) -> Any:
        """A task that notes its inputs, and returns what ``returns`` makes of them"""

        @task(name=name, cache_policy=NO_CACHE)
        def _fake(**kwargs: Any) -> Any:
            calls.setdefault(name, []).append(kwargs)
            return returns(**kwargs) if callable(returns) else returns

        return _fake

    def copy_casda(casda_ms: MS, output_directory: Path, **_: Any) -> MS:
        beam = extract_components_from_name(name=casda_ms.path).beam
        return MS(path=output_directory / f"SB40470.RACS_1237+00.beam{beam}.ms")

    def gaincal(ms: MS | FakeResult, selfcal_round: int, **_: Any) -> MS:
        ms = MS.cast(ms)
        return ms.with_options(
            path=get_selfcal_ms_name(in_ms_path=ms.path, round=selfcal_round)
        )

    def linmos(**_: Any) -> list[Any]:
        submitted.append("linmos")
        return [fake("linmos_images", returns="parset").submit()]

    def cubes(**_: Any) -> list[Any]:
        submitted.append("cubes")
        return []

    science_path = tmp_path / "40470"
    container = tmp_path / "container.sif"
    science_mss = tuple(
        MS(
            path=science_path
            / f"scienceData.RACS_1237+00.SB40470.RACS_1237+00.beam{beam:02d}_averaged_cal.leakage.ms"
        )
        for beam in beams
    )
    gaincal_task = fake("gaincal", returns=gaincal)
    wsclean_task = fake(
        "wsclean", returns=lambda in_ms, **_: FakeResult(MS.cast(in_ms))
    )

    for name, value in {
        "verify_reference_catalogues": lambda reference_directory: True,
        "find_mss": lambda mss_parent_path, expected_ms_count: science_mss,
        "task_copy_and_preprocess_casda_askap_ms": fake("copy", returns=copy_casda),
        "task_describe_ms": fake(
            "describe", returns=lambda ms, attach_ms: FakeSummary(ms)
        ),
        "task_wsclean_imager": SubmitRecorder("image", wsclean_task, submitted),
        "task_gaincal_applycal_ms": SubmitRecorder("selfcal", gaincal_task, submitted),
        "task_create_beam_summary": fake("beam_summary", returns="beam_summary"),
        "task_run_bane_and_aegean": fake("aegean", returns="aegean"),
        "task_update_with_options": fake("update", returns="summary"),
        "task_create_field_summary": fake("field_summary", returns="summary"),
        "task_update_field_summary": fake("update_field_summary", returns="summary"),
        "task_create_image_mask_model": fake("mask", returns="mask"),
        "task_zip_ms": fake("zip"),
        "create_convol_linmos_images": linmos,
        "create_convolve_linmos_cubes": cubes,
        "validation_items": lambda **_: None,
    }.items():
        monkeypatch.setattr(continuum_flow, name, value)
    monkeypatch.setattr(predict_module, "task_create_sky_model", fake("skymodel"))
    monkeypatch.setattr(
        predict_module,
        "task_addmodel_to_ms",
        fake("addmodel", returns=lambda ms, **_: ms),
    )
    monkeypatch.setattr(predict_module, "task_gaincal_applycal_ms", gaincal_task)

    field_options = FieldOptions(
        rounds=2,
        wsclean_container=container,
        casa_container=container,
        calibrate_container=container,
        yandasoft_container=container,
        aegean_container=container,
        run_aegean=True,
        use_beam_masks=True,
        reference_catalogue_directory=tmp_path,
        run_skymodel_calibration=True,
        stokes_v_imaging=True,
        zip_ms=True,
        coadd_cubes=True,
    )
    # The flow fails, and so raises here, should any of its tasks fail
    with prefect_test_harness(), disable_run_logger():
        process_science_fields(
            science_path=science_path,
            split_path=tmp_path / "out",
            field_options=field_options,
        )

    def names(call_name: str, key: str) -> set[str]:
        return {MS.cast(call[key]).path.name for call in calls[call_name]}

    def beam_names(*suffixes: str) -> set[str]:
        return {
            f"SB40470.RACS_1237+00.beam{beam:02d}{suffix}"
            for beam in beams
            for suffix in suffixes
        }

    first_linmos = submitted.index("linmos")
    assert {"image", "selfcal"}.isdisjoint(submitted[first_linmos:])
    assert submitted[-1] == "cubes"

    assert names("gaincal", "ms") == beam_names(".ms", ".round0.ms", ".round1.ms")
    assert {call["selfcal_round"] for call in calls["gaincal"]} == {0, 1, 2}
    # Stokes-I in every round, then Stokes-V through the final Stokes-I results
    assert names("wsclean", "in_ms") == beam_names(
        ".round0.ms", ".round1.ms", ".round2.ms"
    )
    stokes_v = [
        call for call in calls["wsclean"] if isinstance(call["in_ms"], FakeResult)
    ]
    assert {call["in_ms"].ms.path.name for call in stokes_v} == beam_names(".round2.ms")
    assert names("zip", "in_item") == beam_names(".round2.ms")

    (field_summary_call,) = calls["field_summary"]
    assert field_summary_call["mss"] is None
    assert len(field_summary_call["ms_summaries"]) == len(beams)
    assert {
        (call["round"], call["linmos_command"])
        for call in calls["update_field_summary"]
    } == {(None, "parset"), (2, "parset")}


def test_check_field_options_skymodel_requirements(tmp_path, monkeypatch) -> None:
    """run_skymodel_calibration requires a verified reference catalogue directory,
    and both calibrate_container and casa_container"""
    catalogues_valid: list[bool] = []
    monkeypatch.setattr(
        continuum_flow,
        "verify_reference_catalogues",
        lambda reference_directory: catalogues_valid[-1],
    )
    _check_field_options(field_options=FieldOptions(rounds=0))

    container = tmp_path / "container.sif"
    field_options = FieldOptions(
        rounds=0,
        run_skymodel_calibration=True,
        calibrate_container=container,
        casa_container=container,
    )
    with pytest.raises(ValueError, match="reference_catalogue_directory"):
        _check_field_options(field_options=field_options)

    field_options = field_options.with_options(reference_catalogue_directory=tmp_path)
    catalogues_valid.append(False)
    with pytest.raises(ValueError, match="reference_catalogue_directory"):
        _check_field_options(field_options=field_options)

    catalogues_valid.append(True)
    _check_field_options(field_options=field_options)

    for missing in ("calibrate_container", "casa_container"):
        with pytest.raises(ValueError, match=missing):
            _check_field_options(
                field_options=field_options.with_options(**{missing: None})
            )

    with pytest.raises(ValueError, match="Holography cube"):
        _check_field_options(
            field_options=field_options.with_options(holofile=tmp_path / "missing.fits")
        )
