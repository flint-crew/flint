"""Basic tests around addmodel"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from flint.predict.addmodel import (
    AddModelOptions,
    add_model_options_to_command,
    get_parser,
)


def test_get_parser():
    """No silly business around creating the parser (including imports)"""

    _ = get_parser()


def test_create_add_model_instance():
    """Creating the addmodeloptions instance"""

    add_model_options = AddModelOptions(
        model_path=Path("Jack_model.txt"),
        ms_path=Path("Sparrow.ms"),
        mode="a",
        datacolumn="DATA",
    )
    assert isinstance(add_model_options, AddModelOptions)

    with pytest.raises(ValidationError):
        AddModelOptions(
            model_path=Path("Jack_model.txt"),
            ms_path=Path("Sparrow.ms"),
            mode="NoExistsAnRaisesError",
            datacolumn="DATA",
        )


def test_addmodel_to_command_string():
    """make sure we generate the correct command string"""

    add_model_options = AddModelOptions(
        model_path=Path("Jack_model.txt"),
        ms_path=Path("Sparrow.ms"),
        mode="a",
        datacolumn="DATA",
    )

    command = add_model_options_to_command(add_model_options=add_model_options)
    expected = "addmodel -datacolumn DATA -m a Jack_model.txt Sparrow.ms"

    assert command == expected
