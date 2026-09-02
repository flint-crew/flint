"""Standalone RM-synthesis (and, if requested, RM-CLEAN) pipeline: given
already-imaged Stokes Q/U (and optionally I) FITS cubes, write the requested
Faraday dispersion function (FDF) cube and moment map products. See
``flint.rmsynth``.
"""

from __future__ import annotations

from pathlib import Path

from capn_crunch import add_options_to_parser, create_options_from_parser
from configargparse import ArgumentParser
from prefect import flow

from flint.configuration import get_options_from_strategy, load_and_copy_strategy
from flint.convol import BeamShape, common_beam_from_cubes, cubes_share_common_beam
from flint.logging import logger
from flint.naming import create_name_from_common_fields, get_sbid_from_path
from flint.options import (
    BaseOptions,
    FFTBANEOptions,
    RMCleanOptions,
    RMSynthFieldOptions,
    RMSynthOptions,
    StokesCubes,
    StokesErrorCubes,
    StokesNoiseCubes,
)
from flint.prefect.clusters import get_dask_runner
from flint.prefect.common.rmsynth import (
    CommonResolutionCubes,
    convolve_cubes_to_common_resolution,
    task_rmclean,
    task_rmsynth,
    task_write_rm_products,
)
from flint.prefect.common.utils import task_archive_sbid
from flint.rmsynth import needs_rmclean


class RMSynthPipelineResult(BaseOptions):
    """Return value of ``process_rmsynth``, handed in-memory to the
    spice-compression stage of the ``racs-all`` flow-of-flows."""

    written_paths: list[Path]
    """The RM-synthesis and RM-CLEAN products written"""
    convolved_cubes: list[Path]
    """Any new Stokes cubes written to bring the inputs to a common resolution. Empty when the inputs already shared one and were used as they are, so a caller may concatenate these with the input cubes without ever repeating a path"""
    convolved_noise_cubes: list[Path] = []
    """The BANE background and RMS cubes measured on those convolved cubes. Frequency cubes like the rest, so they are trimmed and compressed alongside them. Empty whenever ``convolved_cubes`` is, or when BANE was not asked for"""


def _resolve_common_resolution_cubes(
    stokes_cubes: dict[str, Path],
    output_path: Path | None = None,
    beam_cutoff: float | None = None,
    fft_bane_options: FFTBANEOptions | None = None,
) -> CommonResolutionCubes:
    """RM-synthesis is only meaningful when every channel of every Stokes shares
    one resolution, the 'total' mode of racs_tools. Cubes that already do are
    used as they are; otherwise they are convolved to a common beam and written
    as new cubes, leaving the inputs alone. The polarisation stage hands over
    cubes at a 'natural' resolution, one beam per channel, so in the racs-all
    flow this is the pass that makes them synthesisable while the cubes to
    archive keep their frequency-dependent resolution. The weight cubes are
    untouched, as convolution preserves the pixel grid.

    The convolution runs plane by plane (see
    ``convolve_cubes_to_common_resolution``), so every channel of every Stokes
    is smoothed at once across the cluster.

    Args:
        stokes_cubes (dict[str, Path]): The input cube of each Stokes to run against
        output_path (Path | None, optional): Directory any new cubes are written into. Defaults to alongside the inputs.
        beam_cutoff (float | None, optional): Channels coarser than this, in arcsec, are blanked rather than dragging the common beam out to their resolution. Defaults to no cutoff.
        fft_bane_options (FFTBANEOptions | None, optional): When given, the convolved planes also get BANE background and RMS cubes, measured at the resolution the FDF is built at. Defaults to None.

    Returns:
        CommonResolutionCubes: The cube to use for each Stokes, and any BANE cubes. ``cubes`` is the input dict unchanged when no convolution was needed.
    """
    cube_paths = list(stokes_cubes.values())
    common_beam = common_beam_from_cubes(cube_paths=cube_paths, cutoff=beam_cutoff)
    if common_beam is None:
        logger.warning(
            "No usable restoring beam among the Stokes cubes, so there is no "
            "resolution to make common. Using them as they are."
        )
        return CommonResolutionCubes(cubes=stokes_cubes)

    if cubes_share_common_beam(cube_paths=cube_paths, cutoff=beam_cutoff):
        logger.info("Stokes cubes already share a common resolution")
        return CommonResolutionCubes(cubes=stokes_cubes)

    beam_shape = BeamShape.from_radio_beam(radio_beam=common_beam)
    logger.info(f"Bringing the Stokes cubes to {beam_shape=}")
    return convolve_cubes_to_common_resolution(
        cubes=stokes_cubes,
        beam_shape=beam_shape,
        output_path=output_path,
        beam_cutoff=beam_cutoff,
        fft_bane_options=fft_bane_options,
    )


@flow(name="Flint RM-Synthesis Pipeline")
def process_rmsynth(
    rmsynth_field_options: RMSynthFieldOptions,
) -> RMSynthPipelineResult:
    if rmsynth_field_options.stokes_cubes is None:
        raise ValueError(
            "stokes_cubes is required. The racs-all flow sets it from the "
            "polarisation stage."
        )
    if (
        not rmsynth_field_options.cube_products
        and not rmsynth_field_options.moment_products
        and not rmsynth_field_options.peak_products
    ):
        logger.info("No RM-synthesis products requested, skipping.")
        return RMSynthPipelineResult(written_paths=[], convolved_cubes=[])

    stokes_cube_paths = {
        stokes: cube
        for stokes, cube in (
            ("q", rmsynth_field_options.stokes_cubes.q),
            ("u", rmsynth_field_options.stokes_cubes.u),
            ("i", rmsynth_field_options.stokes_cubes.i),
        )
        if cube is not None
    }

    strategy = load_and_copy_strategy(
        output_split_science_path=rmsynth_field_options.stokes_cubes.q.parent,
        imaging_strategy=rmsynth_field_options.imaging_strategy,
    )

    rmsynth_options = RMSynthOptions(
        **get_options_from_strategy(
            strategy=strategy, operation="rmsynth", mode="rmsynth"
        )
    )
    rmclean_options = RMCleanOptions(
        **get_options_from_strategy(
            strategy=strategy, operation="rmsynth", mode="rmclean"
        )
    )

    common_resolution = _resolve_common_resolution_cubes(
        stokes_cubes=stokes_cube_paths,
        output_path=rmsynth_field_options.output_path,
        beam_cutoff=rmsynth_field_options.beam_cutoff,
        fft_bane_options=rmsynth_field_options.bane_noise,
    )
    stokes_cubes = StokesCubes.from_mapping(common_resolution.cubes)

    # A noise cube only describes the cubes it was measured on. Convolving to a
    # common beam changes them, so the BANE cubes made just above supersede
    # whatever the caller passed in; the caller's are right only when no
    # convolution happened and the cubes are the ones BANE already saw.
    error_cubes: StokesErrorCubes | None = (
        StokesNoiseCubes.from_mapping(common_resolution.rms_cubes)
        if common_resolution.rms_cubes
        else rmsynth_field_options.error_cubes
    )

    synth_result = task_rmsynth.submit(
        stokes_cubes=stokes_cubes,
        rmsynth_options=rmsynth_options,
        error_cubes=error_cubes,
    )

    run_clean = needs_rmclean(
        cube_products=rmsynth_field_options.cube_products,
        moment_products=rmsynth_field_options.moment_products,
        peak_products=rmsynth_field_options.peak_products,
    )
    clean_result = (
        task_rmclean.submit(
            rm_synth_results=synth_result, rmclean_options=rmclean_options
        )
        if run_clean
        else None
    )

    output_prefix = create_name_from_common_fields(
        in_paths=(stokes_cubes.q, stokes_cubes.u)
    )
    if rmsynth_field_options.output_path is not None:
        rmsynth_field_options.output_path.mkdir(parents=True, exist_ok=True)
        output_prefix = rmsynth_field_options.output_path / output_prefix.name

    output_paths = task_write_rm_products.submit(
        synth_results=synth_result,
        clean_results=clean_result,
        stokes_q_cube=stokes_cubes.q,
        rmsynth_options=rmsynth_options,
        rmclean_options=rmclean_options,
        cube_products=rmsynth_field_options.cube_products,
        moment_products=rmsynth_field_options.moment_products,
        peak_products=rmsynth_field_options.peak_products,
        output_prefix=output_prefix,
    )

    written_paths = output_paths.result()

    if rmsynth_field_options.sbid_copy_path:
        task_archive_sbid.submit(
            science_folder_path=output_prefix.parent,
            copy_path=rmsynth_field_options.sbid_copy_path,
        ).result()

    return RMSynthPipelineResult(
        written_paths=written_paths,
        convolved_cubes=list(common_resolution.cubes.values())
        if common_resolution.convolved
        else [],
        convolved_noise_cubes=[
            *common_resolution.bkg_cubes.values(),
            *common_resolution.rms_cubes.values(),
        ],
    )


def setup_run_rmsynth(
    cluster_config: str | Path, rmsynth_field_options: RMSynthFieldOptions
) -> None:
    if (
        rmsynth_field_options.sbid_copy_path
        and rmsynth_field_options.stokes_cubes is not None
    ):
        science_sbid = get_sbid_from_path(path=rmsynth_field_options.stokes_cubes.q)
        rmsynth_field_options = rmsynth_field_options.with_options(
            sbid_copy_path=rmsynth_field_options.sbid_copy_path / f"{science_sbid}"
        )

    dask_task_runner = get_dask_runner(cluster=cluster_config)

    process_rmsynth.with_options(task_runner=dask_task_runner)(
        rmsynth_field_options=rmsynth_field_options
    )


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)

    parser.add_argument(
        "--cli-config", is_config_file=True, help="Path to configuration file"
    )
    parser.add_argument(
        "--cluster-config",
        type=str,
        default="petrichor",
        help="Path to a cluster configuration file, or a known cluster name. ",
    )

    parser = add_options_to_parser(
        parser=parser,
        options_class=RMSynthFieldOptions,
        description="RM-synthesis processing options",
    )

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    rmsynth_field_options = create_options_from_parser(
        parser_namespace=args, options_class=RMSynthFieldOptions
    )

    setup_run_rmsynth(
        cluster_config=args.cluster_config,
        rmsynth_field_options=rmsynth_field_options,
    )


if __name__ == "__main__":
    cli()
