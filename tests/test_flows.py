from __future__ import annotations

import pytest

from flint.logging import logger

from .test_helpers import which

if which("singularity") is None:
    pytest.skip("Singularity is not installed", allow_module_level=True)


def test_singularity():
    which_singularity = which("singularity")
    logger.info(f"Singularity is installed at: {which_singularity}")
    assert which_singularity is not None
