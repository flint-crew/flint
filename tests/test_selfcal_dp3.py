"""Tests for self-calibration with DP3"""

from __future__ import annotations

import h5py
import numpy as np
import pytest
from casacore.tables import table

import flint.selfcal.dp3 as selfcal_dp3
from flint.options import MS
from flint.selfcal.dp3 import dp3_selfcal_ms, get_soltabs, normalise_amplitudes


@pytest.fixture
def selfcal_ms(corrupted_ms: MS) -> MS:
    """The corrupted MS with what flagging before self-calibration expects of real data.

    That flagging takes zeros to be dropped data, so the UVWs are set from the antenna
    positions and the cross-hands are made small but not zero.
    """
    with table(str(corrupted_ms.path / "ANTENNA"), ack=False) as tab:
        positions = tab.getcol("POSITION")
    with table(str(corrupted_ms.path), readonly=False, ack=False) as tab:
        tab.putcol(
            "UVW",
            positions[tab.getcol("ANTENNA2")] - positions[tab.getcol("ANTENNA1")],
        )
        for column in ("DATA", "MODEL_DATA"):
            data = tab.getcol(column)
            data[..., 1:3] = 0.01 * data[..., 0:1]
            tab.putcol(column, data)

    return corrupted_ms


def assert_corrected_matches_model(ms: MS) -> None:
    """Unflagged corrected data matches the model."""
    with table(str(ms.path), ack=False) as tab:
        flags = tab.getcol("FLAG")
        corrected = tab.getcol("CORRECTED_DATA")[~flags]
        model = tab.getcol("MODEL_DATA")[~flags]
    assert corrected.size > 0
    assert np.allclose(corrected, model, atol=1e-3)


def scale_amplitudes(ms: MS, scale: float) -> None:
    """Multiply the data of every baseline by ``scale``, as antenna gains of its square root"""
    with table(str(ms.path), readonly=False, ack=False) as tab:
        tab.putcol("DATA", tab.getcol("DATA") * scale)


def test_dp3_selfcal_ms(selfcal_ms):
    cal_ms = dp3_selfcal_ms(selfcal_ms, selfcal_round=1)

    assert cal_ms.path.name == "SB39400.RACS_0635-31.beam0.small.round1.ms"
    assert cal_ms.column == "CORRECTED_DATA"
    assert cal_ms.h5parm == cal_ms.path.with_suffix(".h5")
    assert get_soltabs(cal_ms.h5parm) == ["phase000"]
    assert_corrected_matches_model(cal_ms)


def test_dp3_selfcal_ms_options(selfcal_ms):
    """Strategy options override the solver's defaults, and gaincal can be used"""
    cal_ms = dp3_selfcal_ms(
        selfcal_ms,
        mode="dp3gaincal",
        update_dp3_options={"caltype": "diagonal"},
    )
    assert get_soltabs(cal_ms.h5parm) == ["amplitude000", "phase000"]


def test_dp3_selfcal_ms_reuses_solutions(selfcal_ms, monkeypatch):
    """An h5parm left by an earlier attempt is applied without solving again"""
    dp3_selfcal_ms(selfcal_ms, selfcal_round=1)

    def no_solve(*_, **__):
        raise AssertionError("Solved again")

    monkeypatch.setattr(selfcal_dp3, "dp3_ddecal", no_solve)
    cal_ms = dp3_selfcal_ms(selfcal_ms, selfcal_round=1)
    assert cal_ms.column == "CORRECTED_DATA"


def test_dp3_selfcal_ms_skip(selfcal_ms):
    cal_ms = dp3_selfcal_ms(selfcal_ms, selfcal_round=2, skip_selfcal=True)
    assert cal_ms.path.name.endswith(".round2.ms")
    assert cal_ms.column == "DATA"
    assert not cal_ms.path.with_suffix(".h5").exists()


@pytest.mark.parametrize("normalise", [True, False])
def test_dp3_selfcal_ms_normalises_amplitudes(selfcal_ms, normalise):
    scale_amplitudes(selfcal_ms, scale=4.0)
    cal_ms = dp3_selfcal_ms(
        selfcal_ms,
        update_dp3_options={"mode": "diagonal"},
        update_dp3_selfcal_options={"normalise_amplitudes": normalise},
    )
    with h5py.File(cal_ms.h5parm) as solutions:
        mean = solutions["sol000/amplitude000/val"][:].mean()
    assert np.isclose(mean, 1.0 if normalise else 2.0, rtol=1e-3)


def test_normalise_amplitudes_is_repeatable(selfcal_ms):
    scale_amplitudes(selfcal_ms, scale=4.0)
    cal_ms = dp3_selfcal_ms(selfcal_ms, update_dp3_options={"mode": "diagonal"})
    with h5py.File(normalise_amplitudes(cal_ms.h5parm)) as solutions:
        mean = solutions["sol000/amplitude000/val"][:].mean()
    assert np.isclose(mean, 1.0, rtol=1e-3)


def test_get_soltabs_missing(tmp_path):
    assert get_soltabs(tmp_path / "missing.h5") == []
    (tmp_path / "broken.h5").write_text("not an h5parm")
    assert get_soltabs(tmp_path / "broken.h5") == []
