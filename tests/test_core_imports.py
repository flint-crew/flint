"""The core modules must import without the pipeline dependencies."""

from __future__ import annotations

import subprocess
import sys
from textwrap import dedent


def test_core_modules_import_without_pipeline_packages() -> None:
    """Import each core module with every pipeline package made unimportable."""
    script = dedent(
        """
    import importlib
    import sys

    blocked = sys.argv[1].split(",")
    for name in blocked:
        sys.modules[name] = None

    for module in sys.argv[2].split(","):
        importlib.import_module(module)

    loaded = [name for name in blocked if sys.modules[name] is not None]
    assert not loaded, f"core pulled in pipeline packages: {loaded}"
    """
    )

    core_modules = (
        "flint",
        "flint.exceptions",
        "flint.logging",
        "flint.naming",
        "flint.options",
        "flint.utils",
    )
    pipeline_packages = (
        "aegeantools",
        "astroquery",
        "billiard",
        "casacore",
        "configargparse",
        "crystalball",
        "dask",
        "dask_jobqueue",
        "distributed",
        "fitscube",
        "fixms",
        "jolly_roger",
        "matplotlib",
        "numba",
        "pandas",
        "prefect",
        "prefect_dask",
        "racs_tools",
        "radio_beam",
        "reproject",
        "rm_lite",
        "rocket_fft",
        "scipy",
        "skimage",
        "spython",
        "zarr",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            ",".join(pipeline_packages),
            ",".join(core_modules),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
