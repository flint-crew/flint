from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from casacore.tables import makecoldesc, table

from flint.utils import get_packaged_resource_path


def pytest_addoption(parser):
    """Add custom flint pytesting options"""
    parser.addoption("--skip-slow", action="store_true")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: mark test as slow to run")


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


def get_columns(ms_path: Path) -> list[str]:
    """The column names of a measurement set"""
    with table(str(ms_path), readonly=True, ack=False) as tab:
        return tab.colnames()


def add_corrected_data(ms_path: Path) -> np.ndarray:
    """Add a CORRECTED_DATA that differs from DATA, the way fixms adds columns, and return it"""
    with table(str(ms_path), readonly=False, ack=False) as tab:
        desc = makecoldesc("DATA", tab.getcoldesc("DATA"))
        desc["name"] = "CORRECTED_DATA"
        tab.addcols(desc)
        # The packaged MS's DATA is all zeros and NaNs, so scaling it would not differ
        corrected_data = tab.getcol("DATA") + (1 + 1j)
        tab.putcol("CORRECTED_DATA", corrected_data)

    return corrected_data


def pytest_collection_modifyitems(config, items):
    if config.getoption("--skip-slow"):
        skip_slow = pytest.mark.skip(reason="need --runslow option to run")
        for item in items:
            if "slow" in item.keywords:
                item.add_marker(skip_slow)

    skip_singularity = pytest.mark.skip(reason="singularity not installed")
    if which("singularity") is None:
        for item in items:
            if "require_singularity" in item.keywords:
                item.add_marker(skip_singularity)


@pytest.fixture
def ms_example(tmpdir):
    ms_zip = Path(
        get_packaged_resource_path(
            package="flint.data.tests",
            filename="SB39400.RACS_0635-31.beam0.small.ms.zip",
        )
    )
    outpath = Path(tmpdir) / "39400"

    shutil.unpack_archive(ms_zip, outpath)

    ms_path = Path(outpath) / "SB39400.RACS_0635-31.beam0.small.ms"

    return ms_path


@pytest.fixture(scope="session")
def flint_containers(tmp_path_factory) -> Path:
    """Download all of the flint containers"""
    from flint.containers import download_known_containers

    flint_container_path = Path(tmp_path_factory.mktemp("download_containers"))
    flint_container_path.mkdir(parents=True, exist_ok=True)

    container_paths = download_known_containers(
        container_directory=flint_container_path, new_tag=None
    )

    assert all(isinstance(path, Path) for path in container_paths)
    assert all(path.exists() for path in container_paths)

    return flint_container_path


@pytest.fixture
def ms_example_with_name(tmpdir):
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
