"""Tests for running DP3 steps"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from casacore.tables import makecoldesc, table
from pydantic import ValidationError

from flint.dp3 import (
    DP3AOFlaggerOptions,
    DP3ApplyCalOptions,
    DP3DDECalOptions,
    DP3GainCalOptions,
    DP3PreFlaggerOptions,
    create_dp3_parset,
    dp3_applycal,
    dp3_ddecal,
    dp3_flag,
    dp3_gaincal,
    dp3_solve_apply,
    get_parser,
    options_to_parset,
)
from flint.options import MS


@pytest.fixture
def corrupted_ms(ms_example: Path) -> MS:
    """The example MS with a unit point source model and known antenna phase errors."""
    with table(str(ms_example), readonly=False, ack=False) as tab:
        desc = makecoldesc("DATA", tab.getcoldesc("DATA"))
        desc["name"] = "MODEL_DATA"
        tab.addcols(desc)

        model = np.zeros(tab.getcol("DATA").shape, dtype=np.complex64)
        model[..., 0] = model[..., 3] = 1
        phases = np.random.default_rng(42).uniform(-1, 1, 36)
        gains = np.exp(
            1j * (phases[tab.getcol("ANTENNA1")] - phases[tab.getcol("ANTENNA2")])
        )

        tab.putcol("MODEL_DATA", model)
        tab.putcol("DATA", (model * gains[:, None, None]).astype(np.complex64))
        tab.putcol("FLAG", np.zeros(model.shape, dtype=bool))

    return MS(path=ms_example, column="DATA", model_column="MODEL_DATA")


def assert_corrected_matches_model(ms: MS) -> None:
    with table(str(ms.path), ack=False) as tab:
        assert not tab.getcol("FLAG").any()
        corrected = tab.getcol(ms.column)
        model = tab.getcol("MODEL_DATA")
    assert np.allclose(corrected, model, atol=1e-3)


def test_options_to_parset():
    parset = options_to_parset(
        "flag",
        DP3PreFlaggerOptions(corrtype="auto", blmax=10.0, chan=["0..7", "280..287"]),
    )
    assert parset == {
        "flag.corrtype": "auto",
        "flag.blmax": "10.0",
        "flag.chan": "[0..7,280..287]",
    }

    parset = options_to_parset("cal", DP3DDECalOptions(propagatesolutions=False))
    assert parset["cal.propagatesolutions"] == "false"
    assert parset["cal.mode"] == "scalarphase"
    assert "cal.maxiter" not in parset


def test_create_dp3_parset():
    ms = MS(path=Path("some.ms"), column="CORRECTED_DATA")
    parset = create_dp3_parset(ms, ["a", "b"], output_column="NEW_DATA")
    assert parset == {
        "msin": "some.ms",
        "msout": ".",
        "steps": "[a,b]",
        "msin.datacolumn": "CORRECTED_DATA",
        "msout.datacolumn": "NEW_DATA",
    }
    assert "msin.datacolumn" not in create_dp3_parset(MS(path=Path("some.ms")), ["a"])


def test_applycal_parset(monkeypatch, tmp_path):
    parsets = []
    monkeypatch.setattr("flint.dp3.run_dp3", parsets.append)
    h5parm = tmp_path / "sol.h5"
    h5parm.touch()

    ms = dp3_applycal(
        MS(path=Path("some.ms")),
        h5parm=h5parm,
        options=DP3ApplyCalOptions(correction=["amplitude000", "phase000"]),
    )

    assert ms.column == "CORRECTED_DATA"
    parset = parsets[0]
    assert parset["applycal.parmdb"] == str(h5parm)
    assert parset["applycal.steps"] == "[c0,c1]"
    assert parset["applycal.c0.correction"] == "amplitude000"
    assert parset["applycal.c1.correction"] == "phase000"
    assert "applycal.correction" not in parset


def test_calibration_needs_model_column():
    with pytest.raises(ValueError, match="No model column"):
        dp3_ddecal(MS(path=Path("some.ms")))


@pytest.mark.parametrize(
    "options_class, kwargs",
    [
        (DP3DDECalOptions, {"mode": "scalarphse"}),
        (DP3DDECalOptions, {"solint": -1}),
        (DP3DDECalOptions, {"maxiter": 0}),
        (DP3DDECalOptions, {"tolerance": 0}),
        (DP3GainCalOptions, {"caltype": "rotation"}),
        (DP3GainCalOptions, {"nchan": -1}),
        (DP3PreFlaggerOptions, {"corrtype": "both"}),
        (DP3PreFlaggerOptions, {"blmin": -1.0}),
        (DP3PreFlaggerOptions, {"blmin": 10.0, "blmax": 5.0}),
        (DP3PreFlaggerOptions, {"uvmmin": 10.0, "uvmmax": 10.0}),
        (DP3PreFlaggerOptions, {"chan": ["7..0"]}),
        (DP3PreFlaggerOptions, {"chan": ["a"]}),
        (DP3PreFlaggerOptions, {"chan": ["0, 1"]}),
        (DP3AOFlaggerOptions, {"strategy": Path("missing.lua")}),
        (DP3AOFlaggerOptions, {"memoryperc": 0}),
        (DP3AOFlaggerOptions, {"timewindow": 0}),
        (DP3ApplyCalOptions, {"correction": []}),
        (DP3ApplyCalOptions, {"correction": ["phase000,amplitude000"]}),
        (DP3ApplyCalOptions, {"solset": "sol 000"}),
        (DP3ApplyCalOptions, {"interpolation": "cubic"}),
    ],
)
def test_invalid_options(options_class, kwargs):
    with pytest.raises(ValidationError):
        options_class(**kwargs)


def test_strategy_must_be_lua(tmp_path):
    strategy = tmp_path / "strategy.txt"
    strategy.touch()
    with pytest.raises(ValidationError, match="lua"):
        DP3AOFlaggerOptions(strategy=strategy)


def test_valid_options():
    DP3PreFlaggerOptions(corrtype="cross", blmin=1.0, blmax=2.0, chan=["0..7", "287"])
    DP3DDECalOptions(mode="tec+phase+delay", solint=0, maxiter=50, tolerance=1e-4)
    DP3GainCalOptions(caltype="fulljones")
    DP3ApplyCalOptions(correction=["rotationmeasure000"], interpolation="linear")


def test_missing_inputs(corrupted_ms, tmp_path):
    with pytest.raises(ValueError, match="NOT_A_COLUMN"):
        dp3_ddecal(corrupted_ms.with_options(model_column="NOT_A_COLUMN"))
    with pytest.raises(ValueError, match="NOT_A_COLUMN"):
        dp3_flag(corrupted_ms.with_options(column="NOT_A_COLUMN"))
    with pytest.raises(FileNotFoundError):
        dp3_applycal(corrupted_ms, tmp_path / "missing.h5")


def test_dp3_flag(corrupted_ms):
    dp3_flag(
        corrupted_ms,
        aoflagger_options=DP3AOFlaggerOptions(),
        preflagger_options=DP3PreFlaggerOptions(corrtype="auto"),
    )

    with table(str(corrupted_ms.path), ack=False) as tab:
        flags = tab.getcol("FLAG")
        autos = tab.getcol("ANTENNA1") == tab.getcol("ANTENNA2")
    assert flags[autos].all()
    assert flags.any()


def test_dp3_ddecal_and_applycal(corrupted_ms):
    h5parm = dp3_ddecal(corrupted_ms, DP3DDECalOptions())
    assert h5parm == corrupted_ms.path.with_suffix(".ddecal.h5")
    assert h5parm.exists()

    ms = dp3_applycal(corrupted_ms, h5parm)
    assert ms.column == "CORRECTED_DATA"
    assert_corrected_matches_model(ms)


def test_dp3_gaincal(corrupted_ms):
    h5parm = dp3_gaincal(corrupted_ms, DP3GainCalOptions())
    assert h5parm == corrupted_ms.path.with_suffix(".gaincal.h5")
    assert h5parm.exists()

    with table(str(corrupted_ms.path), ack=False) as tab:
        assert "CORRECTED_DATA" not in tab.colnames()


@pytest.mark.parametrize("options", [DP3DDECalOptions(), DP3GainCalOptions()])
def test_dp3_solve_apply(corrupted_ms, options):
    ms = dp3_solve_apply(corrupted_ms, options)
    assert ms.column == "CORRECTED_DATA"
    assert_corrected_matches_model(ms)


def test_dp3_finds_measures_data(corrupted_ms):
    """DP3 runs in a fresh interpreter that has not imported casacore itself"""
    env = {
        k: v for k, v in os.environ.items() if k not in ("AIPSPATH", "CASACORE_DATADIR")
    }
    code = (
        "from flint.dp3 import dp3_ddecal; from flint.options import MS; "
        f"dp3_ddecal(MS(path='{corrupted_ms.path}', model_column='MODEL_DATA'))"
    )

    subprocess.run([sys.executable, "-c", code], env=env, check=True)


def test_cli_parser():
    args = get_parser().parse_args(["ddecal", "some.ms", "--mode", "diagonal"])
    assert args.task == "ddecal"
    assert args.mode == "diagonal"
