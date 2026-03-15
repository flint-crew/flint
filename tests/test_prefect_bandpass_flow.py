"""Tests that are specific to the bandpass calibration
flow"""

from __future__ import annotations

from pathlib import Path

from capn_crunch import create_options_from_parser

from flint.options import BandpassOptions
from flint.prefect.flows import bandpass_pipeline


def test_bandpass_cli():
    """Ensure that the bandpass calibration using the BaseOptions
    class can integrate with the current preferred workflow
    used by racs-low3"""
    parser = bandpass_pipeline.get_parser()

    example_cli = """
    /some/test/argument
    --flagger-container /jack/sparrow/containers/aoflagger.sif
    --calibrate-container /jack/sparrow/containers/calibrate.sif
    --cluster-config ./petrichor.yaml
    --split-path /another/made/up/path
    --flag-calibrate-rounds 4
    --minuv 600
    --preflagger-jones-max-amplitude 0.6
    --preflagger-ant-mean-tolerance 0.18
    """
    args = parser.parse_args(example_cli.split())

    bandpass_options = create_options_from_parser(
        parser_namespace=args, options_class=BandpassOptions
    )
    assert args.bandpass_path == Path("/some/test/argument")
    assert args.split_path == Path("/another/made/up/path")
    assert args.cluster_config == "./petrichor.yaml"
    assert bandpass_options.flagger_container == Path(
        "/jack/sparrow/containers/aoflagger.sif"
    )
    assert bandpass_options.calibrate_container == Path(
        "/jack/sparrow/containers/calibrate.sif"
    )
    assert bandpass_options.minuv == 600.0
    assert isinstance(bandpass_options.minuv, float)
    assert not bandpass_options.preflagger_mesh_ant_flags
    assert bandpass_options.flag_calibrate_rounds == 4
    assert isinstance(bandpass_options.flag_calibrate_rounds, int)
