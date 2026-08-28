"""The RACS-All flow-of-flows: chains continuum imaging/self-cal,
polarisation imaging, RM-synthesis/RM-CLEAN and SPICE compression together,
each stage individually skippable and dask-cluster-configurable. Data is
handed from one stage to the next in-memory rather than rediscovered on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from capn_crunch import BaseOptions, add_options_to_parser, create_options_from_parser
from configargparse import ArgumentParser
from fitscube.combine_fits import compress_cube
from prefect import flow
from pydantic import create_model

from flint.configuration import get_options_from_strategy, load_strategy_yaml
from flint.logging import logger
from flint.naming import get_sbid_from_path
from flint.options import (
    FitsCubeOptions,
    PolFieldOptions,
    RACSAllOptions,
    RACSAllPipelineOptions,
    RMSynthFieldOptions,
    SpiceFieldOptions,
    pol_field_options_cli_class,
)
from flint.prefect.clusters import get_dask_runner
from flint.prefect.flows.polarisation_pipeline import process_science_fields_pol
from flint.prefect.flows.racs_all_continuum_selfcal import process_racs_all_continuum
from flint.prefect.flows.rmsynth_pipeline import process_rmsynth
from flint.prefect.flows.spice_compression_pipeline import process_spice_compression

STAGE_CLUSTER_CONFIG_ATTRS = (
    "imaging_cluster_config",
    "polarisation_cluster_config",
    "rmsynth_cluster_config",
    "spice_cluster_config",
)

# Fields on the rm-synth/spice options classes that process_racs_all always recomputes
# from the polarisation stage's output. Excluded from the combined CLI.
COMPUTED_FIELDS = {"stokes_q_cube", "stokes_u_cube", "cubes"}


def _check_stage_prerequisites(
    pipeline_options: RACSAllPipelineOptions,
    racs_all_options: RACSAllOptions,
    pol_field_options: PolFieldOptions,
    spice_field_options: SpiceFieldOptions,
) -> None:
    """Prerequisites of every enabled stage, checked before the first stage runs.

    The stage flows keep their own checks so they stay independently runnable.
    Repeating the checks here turns a missing container into a failure in seconds
    rather than one after imaging and polarisation have already completed.

    Raises:
        ValueError: A required option is unset, or a supplied path does not exist
    """
    paths: dict[str, Path | None] = {}

    if not pipeline_options.skip_imaging:
        paths |= {
            "flagger_container": racs_all_options.flagger_container,
            "casa_container": racs_all_options.casa_container,
            "wsclean_container": racs_all_options.wsclean_container,
            "yandasoft_container": racs_all_options.yandasoft_container,
            "potato_container": racs_all_options.potato_container,
        }

    if not pipeline_options.skip_polarisation:
        for name in ("wsclean_container", "yandasoft_container"):
            if getattr(pol_field_options, name) is None:
                raise ValueError(f"polarisation stage requires {name}")
            paths[f"polarisation {name}"] = getattr(pol_field_options, name)

    if not pipeline_options.skip_spice:
        if spice_field_options.catalogue is None:
            if spice_field_options.aegean_container is None:
                raise ValueError(
                    "spice stage without a catalogue requires aegean_container for "
                    "source finding. Pass --skip-spice to disable the stage."
                )
            paths["aegean_container"] = spice_field_options.aegean_container
        else:
            paths["spice catalogue"] = spice_field_options.catalogue

    for name, path in paths.items():
        if path is not None and not path.exists():
            raise ValueError(f"{name} is set to {path}, which does not exist")


def _check_racs_all_pipeline_options(pipeline_options: RACSAllPipelineOptions) -> None:
    """Enforce the fixed stage order for RACS-all

    A downstream stage may not be enabled while the upstream stage it depends on
    (for its in-memory input) is skipped.
    """
    if not pipeline_options.skip_polarisation and pipeline_options.skip_imaging:
        raise ValueError(
            "polarisation stage requires the imaging stage (cannot set skip_imaging "
            "without skip_polarisation). To run polarisation on its own, use "
            "flint_flow_polarisation_pipeline."
        )
    if not pipeline_options.skip_rmsynth and pipeline_options.skip_polarisation:
        raise ValueError(
            "rm-synth stage requires the polarisation stage (cannot set "
            "skip_polarisation without skip_rmsynth). To run rm-synth on its own, "
            "use flint_flow_rmsynth_pipeline."
        )
    if not pipeline_options.skip_spice and pipeline_options.skip_polarisation:
        raise ValueError(
            "spice stage requires the polarisation stage (cannot set "
            "skip_polarisation without skip_spice). To run spice on its own, use "
            "flint_flow_spice_compression_pipeline."
        )


def _check_spice_mfs_dependency(
    pipeline_options: RACSAllPipelineOptions,
    racs_all_options: RACSAllOptions,
    spice_field_options: SpiceFieldOptions,
) -> None:
    """Aegean source finding (no user catalogue) needs a Stokes I MFS reference image,
    which only exists if the 'total' polarisation strategy sets
    ``flint_save_mfs_products``.
    """
    if pipeline_options.skip_spice or spice_field_options.catalogue is not None:
        return

    strategy = (
        load_strategy_yaml(input_yaml=racs_all_options.imaging_strategy)
        if racs_all_options.imaging_strategy is not None
        else None
    )
    save_mfs_products = get_options_from_strategy(
        strategy=strategy,
        operation="polarisation",
        mode="wsclean",
        polarisation="total",
    ).get("flint_save_mfs_products", False)
    if not save_mfs_products:
        raise ValueError(
            "spice stage with no spice_field_options.catalogue requires the "
            "'total' polarisation strategy to set flint_save_mfs_products=True "
            "(needed as the aegean source-finding reference image). Pass "
            "--skip-spice to disable the stage."
        )


def _exclude_fields_cli_class(
    options_class: type[BaseOptions], exclude_fields: set[str]
) -> type[BaseOptions]:
    """Build a sibling of ``options_class`` exposing only the fields not in
    ``exclude_fields``, so it can be added to a parser that already exposes
    those fields via another options class without a duplicate-flag error.

    The result derives from ``options_class.__base__``, so it is a sibling rather
    than a subclass. Generalises ``pol_field_options_cli_class`` to an arbitrary,
    accumulated set of already-registered field names.
    """
    unique_fields = {
        name: (field.annotation, field)
        for name, field in options_class.model_fields.items()
        if name not in exclude_fields
    }
    return create_model(
        f"{options_class.__name__}CLI",
        __base__=options_class.__base__,
        **unique_fields,
    )


@flow(name="Flint RACS-All Pipeline")
def process_racs_all(
    pipeline_options: RACSAllPipelineOptions,
    racs_all_options: RACSAllOptions,
    pol_field_options: PolFieldOptions,
    rmsynth_field_options: RMSynthFieldOptions,
    spice_field_options: SpiceFieldOptions,
) -> list[Any]:
    _check_racs_all_pipeline_options(pipeline_options=pipeline_options)
    _check_spice_mfs_dependency(
        pipeline_options=pipeline_options,
        racs_all_options=racs_all_options,
        spice_field_options=spice_field_options,
    )
    _check_stage_prerequisites(
        pipeline_options=pipeline_options,
        racs_all_options=racs_all_options,
        pol_field_options=pol_field_options,
        spice_field_options=spice_field_options,
    )

    terminal_results: list[Any] = []

    if not pipeline_options.skip_imaging:
        assert pipeline_options.imaging_cluster_config is not None
        continuum_result = process_racs_all_continuum.with_options(
            task_runner=get_dask_runner(
                cluster=pipeline_options.imaging_cluster_config
            ),
            name="RACS All -- continuum imaging",
        )(racs_all_options=racs_all_options)
        terminal_results.extend(continuum_result.terminal_futures)

    if pipeline_options.skip_polarisation:
        return terminal_results

    resolved_pol_field_options = pol_field_options.with_options(
        holofile=continuum_result.holography_path
    )

    # Compression comes last: the rm-synth and spice stages both read the Stokes
    # cubes chunk-by-chunk, and astropy cannot memmap a gzip file, so each read
    # would decompress the whole cube into memory. Spice compresses whatever it
    # trims, so the pol cubes are only compressed below when spice is skipped.
    pol_fitscube_options = FitsCubeOptions().with_options(
        **get_options_from_strategy(
            strategy=load_strategy_yaml(input_yaml=racs_all_options.imaging_strategy)
            if racs_all_options.imaging_strategy is not None
            else None,
            operation="polarisation",
            mode="fitscube",
        )
    )
    defer_compression = not (
        pipeline_options.skip_rmsynth and pipeline_options.skip_spice
    )

    assert pipeline_options.polarisation_cluster_config is not None
    pol_result = process_science_fields_pol.with_options(
        task_runner=get_dask_runner(
            cluster=pipeline_options.polarisation_cluster_config
        ),
        name="RACS All -- polarisation imaging",
    )(
        flint_ms_directory=continuum_result.output_science_path,
        pol_field_options=resolved_pol_field_options,
        mss_by_beam=continuum_result.mss_by_beam,
        compress_cubes=False if defer_compression else None,
    )
    terminal_results.extend(pol_result.terminal_futures)

    # One root for every downstream stage, split into a subdirectory per stage.
    output_root = pipeline_options.output_path or continuum_result.output_science_path

    if not pipeline_options.skip_rmsynth:
        resolved_rmsynth_field_options = rmsynth_field_options.with_options(
            stokes_q_cube=pol_result.stokes_cubes["q"],
            stokes_u_cube=pol_result.stokes_cubes["u"],
            stokes_i_cube=pol_result.stokes_cubes.get("i"),
            stokes_i_weight_cube=pol_result.weight_cubes.get("i"),
            stokes_q_weight_cube=pol_result.weight_cubes["q"],
            stokes_u_weight_cube=pol_result.weight_cubes["u"],
            output_path=rmsynth_field_options.output_path or output_root / "rmsynth",
        )
        assert pipeline_options.rmsynth_cluster_config is not None
        rmsynth_results = process_rmsynth.with_options(
            task_runner=get_dask_runner(
                cluster=pipeline_options.rmsynth_cluster_config
            ),
            name="RACS All -- rm-synthesis",
        )(rmsynth_field_options=resolved_rmsynth_field_options)
        terminal_results.extend(rmsynth_results)

    if not pipeline_options.skip_spice:
        resolved_reference_image = (
            spice_field_options.reference_image
            if spice_field_options.catalogue is not None
            else pol_result.mfs_products.get("i", {}).get("image")
        )
        resolved_spice_field_options = spice_field_options.with_options(
            cubes=list(pol_result.stokes_cubes.values()),
            reference_image=resolved_reference_image,
            output_path=spice_field_options.output_path or output_root / "spice",
        )
        assert pipeline_options.spice_cluster_config is not None
        spice_results = process_spice_compression.with_options(
            task_runner=get_dask_runner(cluster=pipeline_options.spice_cluster_config),
            name="RACS All -- spice compression",
        )(spice_field_options=resolved_spice_field_options)
        terminal_results.extend(spice_results)
    elif defer_compression and pol_fitscube_options.compress:
        logger.info(
            "Compressing the polarisation cubes, now that rm-synth has read them"
        )
        terminal_results.extend(
            compress_cube(
                stokes_cube,
                method=pol_fitscube_options.compress_method,
                max_workers=pol_fitscube_options.max_workers,
            )
            for stokes_cube in pol_result.stokes_cubes.values()
        )

    return terminal_results


def setup_run_racs_all(
    pipeline_options: RACSAllPipelineOptions,
    racs_all_options: RACSAllOptions,
    pol_field_options: PolFieldOptions,
    rmsynth_field_options: RMSynthFieldOptions,
    spice_field_options: SpiceFieldOptions,
) -> None:
    low_sbid = get_sbid_from_path(path=racs_all_options.low_data)

    process_racs_all.with_options(name=f"RACS All Pipeline -- {low_sbid}")(
        pipeline_options=pipeline_options,
        racs_all_options=racs_all_options,
        pol_field_options=pol_field_options,
        rmsynth_field_options=rmsynth_field_options,
        spice_field_options=spice_field_options,
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

    parser = add_options_to_parser(parser=parser, options_class=RACSAllPipelineOptions)
    parser = add_options_to_parser(parser=parser, options_class=RACSAllOptions)
    seen_fields = (
        set(RACSAllPipelineOptions.model_fields)
        | set(RACSAllOptions.model_fields)
        | COMPUTED_FIELDS
    )

    parser = add_options_to_parser(
        parser=parser,
        options_class=pol_field_options_cli_class(RACSAllOptions),
        description="Polarisation processing options",
    )
    seen_fields |= set(PolFieldOptions.model_fields)

    parser = add_options_to_parser(
        parser=parser,
        options_class=_exclude_fields_cli_class(RMSynthFieldOptions, seen_fields),
        description="RM-synthesis processing options",
    )
    seen_fields |= set(RMSynthFieldOptions.model_fields)

    parser = add_options_to_parser(
        parser=parser,
        options_class=_exclude_fields_cli_class(SpiceFieldOptions, seen_fields),
        description="SPICE compression processing options",
    )

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    # Excluded from the parser (COMPUTED_FIELDS), but create_options_from_parser
    # reads every field off the namespace. process_racs_all sets the real values.
    for field in COMPUTED_FIELDS:
        setattr(args, field, [] if field == "cubes" else None)

    pipeline_options = create_options_from_parser(
        parser_namespace=args, options_class=RACSAllPipelineOptions
    )
    racs_all_options = create_options_from_parser(
        parser_namespace=args, options_class=RACSAllOptions
    )
    pol_field_options = create_options_from_parser(
        parser_namespace=args, options_class=PolFieldOptions
    )
    rmsynth_field_options = create_options_from_parser(
        parser_namespace=args, options_class=RMSynthFieldOptions
    )
    spice_field_options = create_options_from_parser(
        parser_namespace=args, options_class=SpiceFieldOptions
    )

    for cluster_config_attr in STAGE_CLUSTER_CONFIG_ATTRS:
        if getattr(pipeline_options, cluster_config_attr) is None:
            pipeline_options = pipeline_options.with_options(
                **{cluster_config_attr: args.cluster_config}
            )

    setup_run_racs_all(
        pipeline_options=pipeline_options,
        racs_all_options=racs_all_options,
        pol_field_options=pol_field_options,
        rmsynth_field_options=rmsynth_field_options,
        spice_field_options=spice_field_options,
    )


if __name__ == "__main__":
    cli()
