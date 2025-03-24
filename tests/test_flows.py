from __future__ import annotations

import pytest

from flint.logging import logger


# Stolen from: https://stackoverflow.com/questions/377017/test-if-executable-exists-in-python
def which(program: str) -> str | None:
    import os

    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(program)
    if fpath:
        if is_exe(program):
            return program
    else:
        for path in os.environ.get("PATH", "").split(os.pathsep):
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return exe_file

    return None


if which("singularity") is None:
    pytest.skip("Singularity is not installed", allow_module_level=True)


def test_singularity():
    which_singularity = which("singularity")
    logger.info(f"Singularity is installed at: {which_singularity}")
    assert which_singularity is not None
