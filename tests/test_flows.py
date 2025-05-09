from __future__ import annotations

import pytest

from flint.logging import logger

from .conftest import which


@pytest.mark.require_singularity
def test_singularity():
    which_singularity = which("singularity")
    logger.info(f"Singularity is installed at: {which_singularity}")
    assert which_singularity is not None
