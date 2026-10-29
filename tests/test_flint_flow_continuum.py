from __future__ import annotations

import pytest

from flint.options import FieldOptions
from flint.prefect.flows.continuum_pipeline import _check_field_options


def test_check_field_options_casa_container(tmp_path) -> None:
    """Self-calibration needs a casa_container that exists"""
    casa_container = tmp_path / "casa.sif"
    field_options = FieldOptions(rounds=1, casa_container=casa_container)
    with pytest.raises(ValueError, match="casa_container"):
        _check_field_options(field_options=field_options)

    casa_container.touch()
    _check_field_options(field_options=field_options)
