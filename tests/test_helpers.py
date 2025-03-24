from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from flint.utils import get_packaged_resource_path


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
