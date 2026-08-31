"""Tests around the racs-all flow-of-flows"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

import pytest
from capn_crunch import create_options_from_parser
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness

from flint.options import (
    PolFieldOptions,
    RACSAllOptions,
    RACSAllPipelineOptions,
    RMSynthFieldOptions,
    SpiceFieldOptions,
)
from flint.prefect.flows.racs_all_pipeline import (
    _check_racs_all_pipeline_options,
    _check_spice_mfs_dependency,
    _check_stage_prerequisites,
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


def test_get_parser_excludes_computed_stage_outputs() -> None:
    """stokes_q_cube/stokes_u_cube (rm-synth) and cubes/weight_cubes (spice) are
    computed from the polarisation stage's output inside process_racs_all, so the
    combined CLI must not force the user to supply dummy positional values
    for them -- only low_data/mid_data/high_data should be positional."""
    args = get_parser().parse_args(["/low", "/mid", "/high"])

    assert not hasattr(args, "stokes_q_cube")
    assert not hasattr(args, "stokes_u_cube")
    assert not hasattr(args, "cubes")
    assert not hasattr(args, "weight_cubes")

    # create_options_from_parser reads every field off the namespace, so cli()
    # sets the computed ones to their empty defaults first, as done here.
    args.stokes_q_cube = None
    args.stokes_u_cube = None
    args.cubes = []
    args.weight_cubes = []

    rmsynth_field_options = create_options_from_parser(
        parser_namespace=args, options_class=RMSynthFieldOptions
    )
    spice_field_options = create_options_from_parser(
        parser_namespace=args, options_class=SpiceFieldOptions
    )
    assert rmsynth_field_options.stokes_q_cube is None
    assert spice_field_options.cubes == []
    assert spice_field_options.weight_cubes == []


@pytest.fixture
def racs_all_options() -> RACSAllOptions:
    return RACSAllOptions(
        low_data=Path("/does/not/exist/low"),
        mid_data=Path("/does/not/exist/mid"),
        high_data=Path("/does/not/exist/high"),
    )


def test_check_racs_all_pipeline_options_polarisation_requires_imaging() -> None:
    with pytest.raises(ValueError):
        _check_racs_all_pipeline_options(
            RACSAllPipelineOptions(skip_imaging=True, skip_polarisation=False)
        )


def test_check_racs_all_pipeline_options_rmsynth_requires_polarisation() -> None:
    with pytest.raises(ValueError):
        _check_racs_all_pipeline_options(
            RACSAllPipelineOptions(skip_polarisation=True, skip_rmsynth=False)
        )


def test_check_racs_all_pipeline_options_spice_requires_polarisation() -> None:
    with pytest.raises(ValueError):
        _check_racs_all_pipeline_options(
            RACSAllPipelineOptions(
                skip_polarisation=True, skip_rmsynth=True, skip_spice=False
            )
        )


def test_check_racs_all_pipeline_options_default_is_valid() -> None:
    """The default (everything enabled) already respects the stage order."""
    _check_racs_all_pipeline_options(RACSAllPipelineOptions())


def test_check_racs_all_pipeline_options_everything_skipped_is_valid() -> None:
    _check_racs_all_pipeline_options(
        RACSAllPipelineOptions(
            skip_imaging=True,
            skip_polarisation=True,
            skip_rmsynth=True,
            skip_spice=True,
        )
    )


def test_check_spice_mfs_dependency_errors_without_catalogue_or_strategy(
    racs_all_options: RACSAllOptions,
) -> None:
    """No user catalogue and no strategy (so flint_save_mfs_products defaults
    False) means no aegean reference image would be available."""
    with pytest.raises(ValueError):
        _check_spice_mfs_dependency(
            RACSAllPipelineOptions(skip_spice=False),
            racs_all_options,
            SpiceFieldOptions(cubes=[Path("/tmp/i.fits")], catalogue=None),
        )


def test_check_spice_mfs_dependency_ok_with_user_catalogue(
    racs_all_options: RACSAllOptions,
) -> None:
    """A user-supplied catalogue sources WCS/shape from a cube header
    directly, so no MFS reference image check is needed."""
    _check_spice_mfs_dependency(
        RACSAllPipelineOptions(skip_spice=False),
        racs_all_options,
        SpiceFieldOptions(cubes=[Path("/tmp/i.fits")], catalogue=Path("/tmp/cat.fits")),
    )


def test_check_spice_mfs_dependency_ok_when_spice_disabled(
    racs_all_options: RACSAllOptions,
) -> None:
    _check_spice_mfs_dependency(
        RACSAllPipelineOptions(skip_spice=True),
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
        skip_imaging=True, skip_polarisation=True, skip_rmsynth=True, skip_spice=True
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


def test_pol_stage_with_nothing_to_do_still_feeds_rm_synth() -> None:
    """``process_racs_all`` reads ``weight_cubes`` off the polarisation result,
    so a polarisation stage that imaged nothing has to return an empty one
    rather than fail to construct. Both of its give-up paths go through
    ``_no_products``, which is what this pins."""
    from unittest.mock import MagicMock

    from prefect.futures import PrefectFuture

    from flint.prefect.flows.polarisation_pipeline import _no_products

    future = MagicMock(spec=PrefectFuture)
    for result in (_no_products(), _no_products(terminal_futures=[future])):
        assert result.stokes_cubes == {}
        assert result.weight_cubes == {}
        assert result.mfs_products == {}

    # The give-up path that has already built a field summary still propagates it
    assert _no_products().terminal_futures == []
    assert _no_products(terminal_futures=[future]).terminal_futures == [future]


def test_pol_stage_without_a_strategy_returns_an_empty_result(tmp_path: Path) -> None:
    """The give-up path that is reachable without any imaging setup, run through
    the real flow rather than through ``_no_products`` directly: returning an
    empty result is only useful if the flow actually gets there instead of
    raising on the way, which is how the missing ``weight_cubes`` surfaced."""
    from prefect.logging import disable_run_logger

    from flint.options import PolFieldOptions
    from flint.prefect.flows.polarisation_pipeline import process_science_fields_pol

    with prefect_test_harness(), disable_run_logger():
        result = process_science_fields_pol(
            flint_ms_directory=tmp_path,
            pol_field_options=PolFieldOptions(),
        )

    assert result.stokes_cubes == {}
    assert result.weight_cubes == {}
    assert result.mfs_products == {}
    assert result.terminal_futures == []


def test_get_parser_pol_cube_channel_width_is_independent() -> None:
    """The polarisation cube channelisation is deliberately its own option: a
    field name shared with RACSAllOptions is deduplicated in this combined CLI,
    which would silently tie the pol cubes to the continuum grid."""
    args = get_parser().parse_args(
        [
            "/low",
            "/mid",
            "/high",
            "--cube-channel-width",
            "1e6",
            "--pol-cube-channel-width",
            "2e6",
        ]
    )

    pol_field_options = create_options_from_parser(
        parser_namespace=args, options_class=PolFieldOptions
    )
    racs_all_options = create_options_from_parser(
        parser_namespace=args, options_class=RACSAllOptions
    )

    assert racs_all_options.cube_channel_width == 1e6
    assert pol_field_options.pol_cube_channel_width == 2e6


def _containers(tmp_path: Path) -> dict[str, Path]:
    paths = {}
    for name in ("wsclean", "yandasoft", "aegean"):
        path = tmp_path / f"{name}.sif"
        path.touch()
        paths[name] = path
    return paths


def test_stage_prerequisites_accepts_existing_containers(
    tmp_path: Path, racs_all_options: RACSAllOptions
) -> None:
    containers = _containers(tmp_path)
    _check_stage_prerequisites(
        pipeline_options=RACSAllPipelineOptions(skip_imaging=True),
        racs_all_options=racs_all_options,
        pol_field_options=PolFieldOptions(
            wsclean_container=containers["wsclean"],
            yandasoft_container=containers["yandasoft"],
        ),
        spice_field_options=SpiceFieldOptions(aegean_container=containers["aegean"]),
    )


def test_stage_prerequisites_rejects_missing_container_path(
    tmp_path: Path, racs_all_options: RACSAllOptions
) -> None:
    containers = _containers(tmp_path)
    with pytest.raises(ValueError, match="does not exist"):
        _check_stage_prerequisites(
            pipeline_options=RACSAllPipelineOptions(skip_imaging=True),
            racs_all_options=racs_all_options,
            pol_field_options=PolFieldOptions(
                wsclean_container=containers["wsclean"],
                yandasoft_container=tmp_path / "absent.sif",
            ),
            spice_field_options=SpiceFieldOptions(
                aegean_container=containers["aegean"]
            ),
        )


def test_stage_prerequisites_requires_aegean_without_catalogue(
    tmp_path: Path, racs_all_options: RACSAllOptions
) -> None:
    containers = _containers(tmp_path)
    with pytest.raises(ValueError, match="aegean_container"):
        _check_stage_prerequisites(
            pipeline_options=RACSAllPipelineOptions(skip_imaging=True),
            racs_all_options=racs_all_options,
            pol_field_options=PolFieldOptions(
                wsclean_container=containers["wsclean"],
                yandasoft_container=containers["yandasoft"],
            ),
            spice_field_options=SpiceFieldOptions(),
        )


def test_stage_prerequisites_skipped_stages_are_not_checked(
    racs_all_options: RACSAllOptions,
) -> None:
    _check_stage_prerequisites(
        pipeline_options=RACSAllPipelineOptions(
            skip_imaging=True,
            skip_polarisation=True,
            skip_rmsynth=True,
            skip_spice=True,
        ),
        racs_all_options=racs_all_options,
        pol_field_options=PolFieldOptions(),
        spice_field_options=SpiceFieldOptions(),
    )


def test_stage_prerequisites_requires_polarisation_containers(
    racs_all_options: RACSAllOptions,
) -> None:
    with pytest.raises(ValueError, match="polarisation stage requires"):
        _check_stage_prerequisites(
            pipeline_options=RACSAllPipelineOptions(skip_imaging=True, skip_spice=True),
            racs_all_options=racs_all_options,
            pol_field_options=PolFieldOptions(),
            spice_field_options=SpiceFieldOptions(),
        )
