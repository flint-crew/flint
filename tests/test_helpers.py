"""Nothing more than a cmmon place to hold functions
that could be used across many tests"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from flint.utils import get_packaged_resource_path


# Stolen from: https://stackoverflow.com/questions/377017/test-if-executable-exists-in-python
def which(program: str) -> str | None:
    """Locate the program name specified or return None"""
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


@pytest.fixture
def ms_example(tmpdir):
    def _ms_example(output_name: str):
        ms_zip = Path(
            get_packaged_resource_path(
                package="flint.data.tests",
                filename="SB39400.RACS_0635-31.beam0.small.ms.zip",
            )
        )
        outpath = Path(tmpdir) / output_name
        if outpath.exists():
            message = f"{outpath=} already exists. Provide unique {output_name=}"
            raise FileExistsError(message)

        shutil.unpack_archive(ms_zip, outpath)

        return Path(outpath) / "SB39400.RACS_0635-31.beam0.small.ms"

    return _ms_example
