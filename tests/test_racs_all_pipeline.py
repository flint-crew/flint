"""Tests around the racs-all flow-of-flows"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

import pytest
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness

from flint.options import (
    RACSAllOptions,
    RACSAllPipelineOptions,
    SpiceFieldOptions,
)
from flint.prefect.flows.racs_all_pipeline import (
    _check_racs_all_pipeline_options,
    _check_spice_mfs_dependency,
    get_parser,
    process_racs_all,
)


def test_get_parser() -> None:
    """Simple test to ensure that the parser, composed from five options
    classes on one shared parser=, can be built without a duplicate-flag
    error (e.g. from imaging_strategy/sbid_copy_path/aegean_container, which
    are fields shared by more than one of the five classes)."""
    parser = get_parser()
    assert isinstance(parser, ArgumentParser)


@pytest.fixture
def racs_all_options() -> RACSAllOptions:
    return RACSAllOptions(
        low_data=Path("/does/not/exist/low"),
        mid_data=Path("/does/not/exist/mid"),
        high_data=Path("/does/not/exist/high"),
    )


def test_check_racs_all_pipeline_options_run_polarisation_requires_run_imaging() -> (
    None
):
    with pytest.raises(ValueError):
        _check_racs_all_pipeline_options(
            RACSAllPipelineOptions(run_imaging=False, run_polarisation=True)
        )


def test_check_racs_all_pipeline_options_run_rmsynth_requires_run_polarisation() -> (
    None
):
    with pytest.raises(ValueError):
        _check_racs_all_pipeline_options(
            RACSAllPipelineOptions(run_polarisation=False, run_rmsynth=True)
        )


def test_check_racs_all_pipeline_options_run_spice_requires_run_polarisation() -> None:
    with pytest.raises(ValueError):
        _check_racs_all_pipeline_options(
            RACSAllPipelineOptions(
                run_polarisation=False, run_rmsynth=False, run_spice=True
            )
        )


def test_check_racs_all_pipeline_options_default_is_valid() -> None:
    """The default (everything enabled) already respects the stage order."""
    _check_racs_all_pipeline_options(RACSAllPipelineOptions())


def test_check_racs_all_pipeline_options_everything_disabled_is_valid() -> None:
    _check_racs_all_pipeline_options(
        RACSAllPipelineOptions(
            run_imaging=False,
            run_polarisation=False,
            run_rmsynth=False,
            run_spice=False,
        )
    )


def test_check_spice_mfs_dependency_errors_without_catalogue_or_strategy(
    racs_all_options: RACSAllOptions,
) -> None:
    """No user catalogue and no strategy (so flint_save_mfs_products defaults
    False) means no aegean reference image would be available."""
    with pytest.raises(ValueError):
        _check_spice_mfs_dependency(
            RACSAllPipelineOptions(run_spice=True),
            racs_all_options,
            SpiceFieldOptions(cubes=[Path("/tmp/i.fits")], catalogue=None),
        )


def test_check_spice_mfs_dependency_ok_with_user_catalogue(
    racs_all_options: RACSAllOptions,
) -> None:
    """A user-supplied catalogue sources WCS/shape from a cube header
    directly, so no MFS reference image check is needed."""
    _check_spice_mfs_dependency(
        RACSAllPipelineOptions(run_spice=True),
        racs_all_options,
        SpiceFieldOptions(cubes=[Path("/tmp/i.fits")], catalogue=Path("/tmp/cat.fits")),
    )


def test_check_spice_mfs_dependency_ok_when_spice_disabled(
    racs_all_options: RACSAllOptions,
) -> None:
    _check_spice_mfs_dependency(
        RACSAllPipelineOptions(run_spice=False),
        racs_all_options,
        SpiceFieldOptions(cubes=[Path("/tmp/i.fits")], catalogue=None),
    )


def test_process_racs_all_everything_disabled_returns_immediately(
    racs_all_options: RACSAllOptions,
) -> None:
    """The cheapest possible smoke test that stage-skip wiring doesn't blow
    up on the all-disabled edge case."""
    from flint.options import PolFieldOptions, RMSynthFieldOptions

    pipeline_options = RACSAllPipelineOptions(
        run_imaging=False, run_polarisation=False, run_rmsynth=False, run_spice=False
    )
    pol_field_options = PolFieldOptions()
    rmsynth_field_options = RMSynthFieldOptions(
        stokes_q_cube=Path("/tmp/q.fits"), stokes_u_cube=Path("/tmp/u.fits")
    )
    spice_field_options = SpiceFieldOptions(cubes=[Path("/tmp/i.fits")])

    with prefect_test_harness(), disable_run_logger():
        result = process_racs_all(
            pipeline_options=pipeline_options,
            racs_all_options=racs_all_options,
            pol_field_options=pol_field_options,
            rmsynth_field_options=rmsynth_field_options,
            spice_field_options=spice_field_options,
        )

    assert result == []
