"""Tests for self-calibration with DP3"""

from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np
import pytest
from casacore.tables import table

import flint.selfcal.dp3 as selfcal_dp3
from flint.dp3 import DP3DDECalOptions, DP3GainCalOptions
from flint.options import MS
from flint.selfcal.dp3 import (
    dp3_selfcal_ms,
    get_corrections,
    get_soltabs,
    get_solve_type,
    normalise_amplitudes,
)


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


def write_h5parm(
    h5parm: Path, soltabs: dict[str, np.ndarray], pols: list[str] | None = None
) -> Path:
    """An h5parm of the given soltabs, on time and antenna axes, and a polarisation axis if ``pols`` is set."""
    with h5py.File(h5parm, "w") as solutions:
        for name, values in soltabs.items():
            soltab = solutions.create_group(f"sol000/{name}")
            val = soltab.create_dataset("val", data=values)
            val.attrs["AXES"] = np.bytes_("time,ant,pol" if pols else "time,ant")
            soltab.create_dataset("weight", data=np.ones_like(values))
            if pols:
                soltab.create_dataset("pol", data=np.array(pols, dtype="S2"))

    return h5parm


def test_dp3_selfcal_ms(selfcal_ms):
    cal_ms = dp3_selfcal_ms(selfcal_ms, selfcal_round=1)

    assert cal_ms.path.name == "SB39400.RACS_0635-31.beam0.small.round1.ms"
    assert cal_ms.column == "CORRECTED_DATA"
    assert cal_ms.h5parm is None
    assert get_soltabs(cal_ms.path.with_suffix(".h5")) == ["phase000"]
    assert_corrected_matches_model(cal_ms)


def test_dp3_selfcal_ms_options(selfcal_ms):
    """Strategy options override the solver's defaults, and gaincal can be used"""
    cal_ms = dp3_selfcal_ms(
        selfcal_ms,
        mode="dp3gaincal",
        update_dp3_options={"caltype": "diagonal"},
    )
    assert get_soltabs(cal_ms.path.with_suffix(".h5")) == ["amplitude000", "phase000"]


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
    with h5py.File(cal_ms.path.with_suffix(".h5")) as solutions:
        mean = solutions["sol000/amplitude000/val"][:].mean()
    assert np.isclose(mean, 1.0 if normalise else 2.0, rtol=1e-3)


def test_normalise_amplitudes_is_repeatable(selfcal_ms):
    scale_amplitudes(selfcal_ms, scale=4.0)
    cal_ms = dp3_selfcal_ms(selfcal_ms, update_dp3_options={"mode": "diagonal"})
    with h5py.File(normalise_amplitudes(cal_ms.path.with_suffix(".h5"))) as solutions:
        mean = solutions["sol000/amplitude000/val"][:].mean()
    assert np.isclose(mean, 1.0, rtol=1e-3)


def test_get_soltabs_missing(tmp_path):
    assert get_soltabs(tmp_path / "missing.h5") == []
    (tmp_path / "broken.h5").write_text("not an h5parm")
    assert get_soltabs(tmp_path / "broken.h5") == []


def test_dp3_selfcal_ms_fulljones(selfcal_ms):
    cal_ms = dp3_selfcal_ms(selfcal_ms, update_dp3_options={"mode": "fulljones"})
    assert get_soltabs(cal_ms.path.with_suffix(".h5")) == ["amplitude000", "phase000"]
    assert_corrected_matches_model(cal_ms)


def test_get_solve_type():
    assert get_solve_type(DP3DDECalOptions(mode="rotation+diagonal")) == (
        "rotation+diagonal"
    )
    assert get_solve_type(DP3GainCalOptions(caltype="fulljones")) == "fulljones"
    with pytest.raises(ValueError, match="cannot apply leakage"):
        get_solve_type(DP3DDECalOptions(mode="leakage"))


def test_dp3_selfcal_ms_refuses_solves_it_cannot_apply(tmp_path):
    """The solve type is checked before the measurement set is copied"""
    with pytest.raises(ValueError, match="cannot apply"):
        dp3_selfcal_ms(
            MS(path=tmp_path / "missing.ms"), update_dp3_options={"mode": "tec+delay"}
        )


def test_get_corrections_undoes_the_diagonal_before_the_rotation(tmp_path):
    h5parm = write_h5parm(
        tmp_path / "solutions.h5",
        {name: np.ones((2, 3)) for name in ("rotation000", "phase000", "amplitude000")},
    )
    corrections = get_corrections(h5parm, "rotation+diagonal")
    assert corrections.correction == ["amplitude000", "phase000", "rotation000"]


def test_get_corrections_fulljones(tmp_path):
    h5parm = write_h5parm(
        tmp_path / "solutions.h5",
        {"amplitude000": np.ones((2, 3, 4)), "phase000": np.zeros((2, 3, 4))},
        pols=["XX", "XY", "YX", "YY"],
    )
    corrections = get_corrections(h5parm, "fulljones")
    assert corrections.correction == ["fulljones"]
    assert corrections.soltab == ["amplitude000", "phase000"]


def test_normalise_amplitudes_leaves_out_cross_hands(tmp_path):
    """Full-Jones cross-hands are near zero, so would drag the mean down"""
    amplitudes = np.empty((2, 3, 4))
    amplitudes[..., [0, 3]] = 2.0
    amplitudes[..., [1, 2]] = 0.02
    h5parm = write_h5parm(
        tmp_path / "solutions.h5",
        {"amplitude000": amplitudes},
        pols=["XX", "XY", "YX", "YY"],
    )

    with h5py.File(normalise_amplitudes(h5parm)) as solutions:
        normalised = solutions["sol000/amplitude000/val"][:]
    assert np.allclose(normalised[..., [0, 3]], 1.0)
    assert np.allclose(normalised[..., [1, 2]], 0.01)
