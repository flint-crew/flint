"""Tests around the racs-all flow-of-flows"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Any

import pytest
from capn_crunch import create_options_from_parser
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness

from flint.options import (
    CubesForRMSynth,
    NoiseCubesForRMSynth,
    PolFieldOptions,
    RACSAllOptions,
    RACSAllPipelineOptions,
    RMSynthFieldOptions,
    SpiceFieldOptions,
    WeightCubesForRMSynth,
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
    """stokes_cubes/error_cubes (rm-synth) and cubes/weight_cubes (spice) are
    computed from the polarisation stage's output inside process_racs_all, so the
    combined CLI must not force the user to supply dummy positional values
    for them -- only low_data/mid_data/high_data should be positional."""
    args = get_parser().parse_args(["/low", "/mid", "/high"])

    assert not hasattr(args, "stokes_cubes")
    assert not hasattr(args, "error_cubes")
    assert not hasattr(args, "cubes")
    assert not hasattr(args, "weight_cubes")

    # create_options_from_parser reads every field off the namespace, so cli()
    # sets the computed ones to their empty defaults first, as done here.
    args.stokes_cubes = None
    args.error_cubes = None
    args.cubes = []
    args.weight_cubes = []

    rmsynth_field_options = create_options_from_parser(
        parser_namespace=args, options_class=RMSynthFieldOptions
    )
    spice_field_options = create_options_from_parser(
        parser_namespace=args, options_class=SpiceFieldOptions
    )
    assert rmsynth_field_options.stokes_cubes is None
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
        stokes_cubes=CubesForRMSynth(
            q_path=Path("/tmp/q.fits"), u_path=Path("/tmp/u.fits")
        )
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


def _stage_mocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    convolved_cubes: list[Path],
    bane_cubes: bool = False,
):
    """Replace every stage subflow of ``process_racs_all`` with a mock, so the
    wiring between them can be checked without imaging anything."""
    from unittest.mock import MagicMock

    from flint.prefect.flows.polarisation_pipeline import PolPipelineResult
    from flint.prefect.flows.rmsynth_pipeline import RMSynthPipelineResult

    def _stage(result):
        mock = MagicMock()
        mock.with_options.return_value.return_value = result
        return mock

    continuum_result = MagicMock()
    continuum_result.terminal_futures = []
    continuum_result.output_science_path = tmp_path
    continuum_result.holography_path = None

    pol_result = PolPipelineResult(
        stokes_cubes={"q": tmp_path / "q.fits", "u": tmp_path / "u.fits"},
        weight_cubes={"q": tmp_path / "q.weight.fits", "u": tmp_path / "u.weight.fits"},
        bkg_cubes={"q": tmp_path / "q_bkg.fits", "u": tmp_path / "u_bkg.fits"}
        if bane_cubes
        else {},
        rms_cubes={"q": tmp_path / "q_rms.fits", "u": tmp_path / "u_rms.fits"}
        if bane_cubes
        else {},
        mfs_products={},
        terminal_futures=[],
    )
    spice_stage = _stage([])
    rmsynth_stage = _stage(
        RMSynthPipelineResult(
            written_paths=[tmp_path / "fdf.fits"], convolved_cubes=convolved_cubes
        )
    )

    monkeypatch.setattr(
        "flint.prefect.flows.racs_all_pipeline.get_dask_runner", lambda cluster: None
    )
    monkeypatch.setattr(
        "flint.prefect.flows.racs_all_pipeline.process_racs_all_continuum",
        _stage(continuum_result),
    )
    monkeypatch.setattr(
        "flint.prefect.flows.racs_all_pipeline.process_science_fields_pol",
        _stage(pol_result),
    )
    monkeypatch.setattr(
        "flint.prefect.flows.racs_all_pipeline.process_rmsynth", rmsynth_stage
    )
    monkeypatch.setattr(
        "flint.prefect.flows.racs_all_pipeline.process_spice_compression", spice_stage
    )

    return pol_result, spice_stage, rmsynth_stage


def _run_racs_all(tmp_path: Path) -> None:
    container = tmp_path / "container.sif"
    container.touch()
    catalogue = tmp_path / "components.fits"
    catalogue.touch()

    with prefect_test_harness(), disable_run_logger():
        process_racs_all(
            pipeline_options=RACSAllPipelineOptions(
                imaging_cluster_config=tmp_path,
                polarisation_cluster_config=tmp_path,
                rmsynth_cluster_config=tmp_path,
                spice_cluster_config=tmp_path,
            ),
            racs_all_options=RACSAllOptions(
                low_data=tmp_path, mid_data=tmp_path, high_data=tmp_path
            ),
            pol_field_options=PolFieldOptions(
                wsclean_container=container, yandasoft_container=container
            ),
            rmsynth_field_options=RMSynthFieldOptions(),
            spice_field_options=SpiceFieldOptions(catalogue=catalogue),
        )


def test_rmsynth_convolved_cubes_are_spiced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common-resolution cubes rm-synth writes are full-size copies of the
    polarisation cubes, so leaving them out of the spice stage would strand an
    untrimmed, uncompressed set on disk once the originals have been trimmed."""
    convolved_cubes = [tmp_path / "q.conv.fits", tmp_path / "u.conv.fits"]
    pol_result, spice_stage, _ = _stage_mocks(
        monkeypatch=monkeypatch, tmp_path=tmp_path, convolved_cubes=convolved_cubes
    )

    _run_racs_all(tmp_path=tmp_path)

    spiced_options = spice_stage.with_options.return_value.call_args.kwargs[
        "spice_field_options"
    ]
    assert spiced_options.cubes == [
        *pol_result.stokes_cubes.values(),
        *convolved_cubes,
    ]
    # Convolution preserves the pixel grid, so the one weight set serves both
    assert spiced_options.weight_cubes == list(pol_result.weight_cubes.values())


def test_spiced_cubes_are_not_repeated_without_convolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rm-synth reports no convolved cubes when the inputs already shared a
    resolution, so the spice stage never sees the same cube twice."""
    pol_result, spice_stage, _ = _stage_mocks(
        monkeypatch=monkeypatch, tmp_path=tmp_path, convolved_cubes=[]
    )

    _run_racs_all(tmp_path=tmp_path)

    spiced_options = spice_stage.with_options.return_value.call_args.kwargs[
        "spice_field_options"
    ]
    assert spiced_options.cubes == list(pol_result.stokes_cubes.values())
    assert len(set(spiced_options.cubes)) == len(spiced_options.cubes)


def _rmsynth_options(rmsynth_stage):
    """The options the racs-all flow handed the rm-synth stage"""
    return rmsynth_stage.with_options.return_value.call_args.kwargs[
        "rmsynth_field_options"
    ]


def test_bane_rms_cubes_are_preferred_over_the_linmos_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BANE RMS cube is measured off the co-added planes, so it describes the
    cubes rm-synth reads; the linmos weights are an inverse variance carried
    over from the input images. When both exist the RMS cubes win, and the two
    must not be sent together -- rm-lite is told which it has by a single flag.
    """
    _, _, rmsynth_stage = _stage_mocks(
        monkeypatch=monkeypatch, tmp_path=tmp_path, convolved_cubes=[], bane_cubes=True
    )

    _run_racs_all(tmp_path=tmp_path)

    options = _rmsynth_options(rmsynth_stage)
    assert isinstance(options.error_cubes, NoiseCubesForRMSynth)
    assert options.error_cubes.q_path == tmp_path / "q_rms.fits"
    assert options.error_cubes.u_path == tmp_path / "u_rms.fits"


def test_the_linmos_weights_are_used_when_bane_did_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BANE is opt-in, so a polarisation stage that skipped it still has to feed
    rm-synth something."""
    _, _, rmsynth_stage = _stage_mocks(
        monkeypatch=monkeypatch, tmp_path=tmp_path, convolved_cubes=[]
    )

    _run_racs_all(tmp_path=tmp_path)

    options = _rmsynth_options(rmsynth_stage)
    assert isinstance(options.error_cubes, WeightCubesForRMSynth)
    assert options.error_cubes.q_path == tmp_path / "q.weight.fits"


def test_the_bane_cubes_are_spiced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The background and RMS cubes are full-size copies on the polarisation
    cubes' pixel grid. Left out of the spice stage they would stay untrimmed and
    uncompressed on disk after everything beside them had been trimmed."""
    pol_result, spice_stage, _ = _stage_mocks(
        monkeypatch=monkeypatch, tmp_path=tmp_path, convolved_cubes=[], bane_cubes=True
    )

    _run_racs_all(tmp_path=tmp_path)

    spiced = spice_stage.with_options.return_value.call_args.kwargs[
        "spice_field_options"
    ]
    assert spiced.weight_cubes == [
        *pol_result.weight_cubes.values(),
        *pol_result.bkg_cubes.values(),
        *pol_result.rms_cubes.values(),
    ]


def test_rmsynth_refuses_a_polarisation_strategy_without_the_linear_stokes(
    tmp_path: Path,
) -> None:
    """The FDF is built from Q+iU, so a circular-only strategy has nothing to
    give rm-synthesis. Caught up front rather than after imaging has run."""
    from flint.prefect.flows.racs_all_pipeline import _check_rmsynth_has_linear_stokes

    strategy = tmp_path / "polarisation.yaml"

    def _check(**pol_options: Any) -> None:
        _check_rmsynth_has_linear_stokes(
            pipeline_options=RACSAllPipelineOptions(),
            pol_field_options=PolFieldOptions(**pol_options),
        )

    strategy.write_text(
        "version: 0.2\ndefaults: {}\npolarisation:\n  circular:\n    wsclean: {}\n"
    )
    with pytest.raises(ValueError, match="rm-synthesis needs Stokes"):
        _check(imaging_strategy=strategy)

    strategy.write_text(
        "version: 0.2\ndefaults: {}\npolarisation:\n  linear:\n    wsclean: {}\n"
    )
    _check(imaging_strategy=strategy)

    # No strategy is the polarisation stage's business, not this check's
    _check()

    _check_rmsynth_has_linear_stokes(
        pipeline_options=RACSAllPipelineOptions(skip_rmsynth=True),
        pol_field_options=PolFieldOptions(imaging_strategy=strategy),
    )
