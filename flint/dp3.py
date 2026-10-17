"""Run DP3 steps against measurement sets through DP3's Python interface"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

from capn_crunch import add_options_to_parser, create_options_from_parser

from flint.logging import logger
from flint.options import MS, BaseOptions
from flint.utils import get_packaged_resource_path


class DP3PreFlaggerOptions(BaseOptions):
    """Options for the DP3 preflagger step. Unset selections are not used."""

    corrtype: str | None = None
    """Flag only 'auto' or 'cross' correlations"""
    blmin: float | None = None
    """Flag baselines shorter than this length in metres"""
    blmax: float | None = None
    """Flag baselines longer than this length in metres"""
    uvmmin: float | None = None
    """Flag data with a UV distance below this in metres"""
    uvmmax: float | None = None
    """Flag data with a UV distance above this in metres"""
    chan: list[str] | None = None
    """Channels to flag, e.g. ['0..7', '280..287']"""
    amplmin: float | None = None
    """Flag data with an amplitude below this"""
    amplmax: float | None = None
    """Flag data with an amplitude above this"""


class DP3AOFlaggerOptions(BaseOptions):
    """Options for the DP3 aoflagger step"""

    strategy: Path | None = None
    """The aoflagger strategy file. The packaged ASKAP strategy is used if unset."""
    memoryperc: int | None = None
    """Percentage of system memory aoflagger may use"""
    timewindow: int | None = None
    """Number of time slots flagged together"""


class DP3DDECalOptions(BaseOptions):
    """Options for the DP3 ddecal step"""

    mode: str = "scalarphase"
    """What to solve for, e.g. 'scalarphase', 'diagonal' or 'fulljones'"""
    solint: int = 1
    """Number of time slots per solution. 0 uses the whole observation."""
    nchan: int = 0
    """Number of channels per solution. 0 uses the whole band."""
    maxiter: int | None = None
    """Maximum number of solver iterations"""
    tolerance: float | None = None
    """Convergence tolerance of the solver"""
    uvlambdamin: float | None = None
    """Ignore baselines shorter than this many wavelengths"""
    propagatesolutions: bool | None = None
    """Start each solution interval from the previous solutions"""


class DP3GainCalOptions(BaseOptions):
    """Options for the DP3 gaincal step"""

    caltype: str = "scalarphase"
    """What to solve for, e.g. 'scalarphase', 'diagonal' or 'fulljones'"""
    solint: int = 1
    """Number of time slots per solution. 0 uses the whole observation."""
    nchan: int = 0
    """Number of channels per solution. 0 uses the whole band."""
    maxiter: int | None = None
    """Maximum number of solver iterations"""
    tolerance: float | None = None
    """Convergence tolerance of the solver"""
    uvlambdamin: float | None = None
    """Ignore baselines shorter than this many wavelengths"""
    propagatesolutions: bool | None = None
    """Start each solution interval from the previous solutions"""


class DP3ApplyCalOptions(BaseOptions):
    """Options for the DP3 applycal step"""

    correction: list[str] = ["phase000"]
    """Soltabs to apply in turn, e.g. ['phase000'] or ['rotationmeasure000']"""
    solset: str | None = None
    """The solset holding the soltabs. DP3 picks one if unset."""
    interpolation: str | None = None
    """How solutions are interpolated, 'nearest' or 'linear'"""
    updateweights: bool | None = None
    """Update the weights along with the data"""
    invert: bool | None = None
    """Apply the inverse of the solutions, i.e. correct the data"""


def options_to_parset(step: str, options: BaseOptions) -> dict[str, str]:
    """Turn step options into DP3 parset keys and values, skipping unset ones."""
    parset = {}
    for key, value in options._asdict().items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = str(value).lower()
        elif isinstance(value, (list, tuple)):
            value = "[" + ",".join(str(v) for v in value) + "]"
        parset[f"{step}.{key}"] = str(value)

    return parset


def create_dp3_parset(
    ms: MS, steps: list[str], output_column: str | None = None
) -> dict[str, str]:
    """The parset to read a measurement set and update it in place."""
    parset = {"msin": str(ms.path), "msout": ".", "steps": f"[{','.join(steps)}]"}
    if ms.column:
        parset["msin.datacolumn"] = ms.column
    if output_column:
        parset["msout.datacolumn"] = output_column

    return parset


def run_dp3(parset: dict[str, str]) -> None:
    """Run a DP3 pipeline described by parset keys and values."""
    # DP3 ships no measures data. Importing casacore first points AIPSPATH at
    # python-casacore's copy, unless one is already set.
    import casacore  # noqa: F401
    import dp3
    import dp3.parameterset

    logger.info(f"Running DP3 with {parset=}")
    parameter_set = dp3.parameterset.ParameterSet()
    for key, value in parset.items():
        parameter_set.add(key, value)

    first_step = dp3.make_main_steps(parameter_set)
    first_step.set_info(dp3.DPInfo())
    while first_step.process(dp3.DPBuffer()):
        pass
    first_step.finish()


def get_model_column(ms: MS) -> str:
    """The model column of a measurement set, which calibration needs."""
    if ms.model_column is None:
        raise ValueError(f"No model column set for {ms.path}")

    return ms.model_column


def dp3_flag(
    ms: MS | Path,
    aoflagger_options: DP3AOFlaggerOptions | None = None,
    preflagger_options: DP3PreFlaggerOptions | None = None,
) -> MS:
    """Flag a measurement set with aoflagger, after the preflagger if given."""
    ms = MS.cast(ms)
    aoflagger_options = aoflagger_options or DP3AOFlaggerOptions()
    if aoflagger_options.strategy is None:
        aoflagger_options = aoflagger_options.with_options(
            strategy=get_packaged_resource_path(
                package="flint.data.aoflagger", filename="ASKAP.lua"
            )
        )

    steps = ["aoflagger"]
    parset = options_to_parset("aoflagger", aoflagger_options)
    if preflagger_options:
        steps.insert(0, "preflagger")
        parset |= options_to_parset("preflagger", preflagger_options)

    run_dp3(create_dp3_parset(ms, steps) | parset)

    return ms


def dp3_ddecal(
    ms: MS | Path, options: DP3DDECalOptions | None = None, h5parm: Path | None = None
) -> Path:
    """Solve for gains against the model column with ddecal, returning the h5parm."""
    ms = MS.cast(ms)
    h5parm = h5parm or ms.path.with_suffix(".ddecal.h5")

    run_dp3(
        create_dp3_parset(ms, ["ddecal"])
        | options_to_parset("ddecal", options or DP3DDECalOptions())
        | {
            "ddecal.modeldatacolumns": f"[{get_model_column(ms)}]",
            "ddecal.h5parm": str(h5parm),
        }
    )

    return h5parm


def dp3_gaincal(
    ms: MS | Path,
    options: DP3GainCalOptions | None = None,
    h5parm: Path | None = None,
    output_column: str | None = None,
) -> Path:
    """Solve for gains against the model column with gaincal, returning the h5parm.

    The solutions are also applied in the same pass if ``output_column`` is set.
    """
    ms = MS.cast(ms)
    h5parm = h5parm or ms.path.with_suffix(".gaincal.h5")

    parset = create_dp3_parset(ms, ["gaincal"], output_column=output_column)
    parset |= options_to_parset("gaincal", options or DP3GainCalOptions())
    parset |= {
        "gaincal.usemodelcolumn": "true",
        "gaincal.modelcolumn": get_model_column(ms),
        "gaincal.parmdb": str(h5parm),
        "gaincal.applysolution": str(output_column is not None).lower(),
    }
    run_dp3(parset)

    return h5parm


def dp3_applycal(
    ms: MS | Path,
    h5parm: Path,
    options: DP3ApplyCalOptions | None = None,
    output_column: str = "CORRECTED_DATA",
) -> MS:
    """Apply the solutions in an h5parm, writing the corrected data to ``output_column``."""
    ms = MS.cast(ms)
    options = options or DP3ApplyCalOptions()

    # Each soltab gets its own sub-step so several can be applied in one pass
    corrections = [f"c{i}" for i in range(len(options.correction))]
    parset = create_dp3_parset(ms, ["applycal"], output_column=output_column)
    parset |= {
        key: value
        for key, value in options_to_parset("applycal", options).items()
        if key != "applycal.correction"
    }
    parset |= {
        "applycal.parmdb": str(h5parm),
        "applycal.steps": f"[{','.join(corrections)}]",
    }
    for name, soltab in zip(corrections, options.correction):
        parset[f"applycal.{name}.correction"] = soltab
    run_dp3(parset)

    return ms.with_options(column=output_column)


def dp3_solve_apply(
    ms: MS | Path,
    options: DP3DDECalOptions | DP3GainCalOptions | None = None,
    h5parm: Path | None = None,
    output_column: str = "CORRECTED_DATA",
) -> MS:
    """Solve against the model column and apply the solutions to ``output_column``."""
    ms = MS.cast(ms)
    options = options or DP3DDECalOptions()

    if isinstance(options, DP3GainCalOptions):
        dp3_gaincal(ms, options, h5parm=h5parm, output_column=output_column)
        return ms.with_options(column=output_column)

    # DDECal can not reliably hand its solutions to applycal in the same pass
    h5parm = dp3_ddecal(ms, options, h5parm=h5parm)
    return dp3_applycal(ms, h5parm, output_column=output_column)


def get_parser() -> ArgumentParser:
    """Create the argument parser for the DP3 tasks."""
    parser = ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="task")

    flag_parser = subparsers.add_parser("flag", help="Flag with aoflagger")
    flag_parser.add_argument("ms", type=Path, help="The measurement set to flag")
    flag_parser.add_argument("--column", default="DATA", help="The column to flag")
    add_options_to_parser(parser=flag_parser, options_class=DP3AOFlaggerOptions)

    for task, options_class in (
        ("ddecal", DP3DDECalOptions),
        ("gaincal", DP3GainCalOptions),
    ):
        solve_parser = subparsers.add_parser(task, help=f"Solve for gains with {task}")
        solve_parser.add_argument("ms", type=Path, help="The measurement set to solve")
        solve_parser.add_argument("--column", default="DATA", help="The data column")
        solve_parser.add_argument(
            "--model-column", default="MODEL_DATA", help="The model column"
        )
        solve_parser.add_argument("--h5parm", type=Path, help="The output h5parm")
        add_options_to_parser(parser=solve_parser, options_class=options_class)

    apply_parser = subparsers.add_parser("applycal", help="Apply an h5parm")
    apply_parser.add_argument("ms", type=Path, help="The measurement set to correct")
    apply_parser.add_argument("h5parm", type=Path, help="The h5parm to apply")
    apply_parser.add_argument("--column", default="DATA", help="The data column")
    apply_parser.add_argument(
        "--output-column", default="CORRECTED_DATA", help="The corrected data column"
    )
    add_options_to_parser(parser=apply_parser, options_class=DP3ApplyCalOptions)

    return parser


def cli() -> None:
    parser = get_parser()
    args = parser.parse_args()

    if args.task is None:
        parser.print_help()
        return

    ms = MS(path=args.ms, column=args.column)
    if args.task == "flag":
        dp3_flag(
            ms,
            aoflagger_options=create_options_from_parser(
                parser_namespace=args, options_class=DP3AOFlaggerOptions
            ),
        )
    elif args.task == "ddecal":
        dp3_ddecal(
            ms.with_options(model_column=args.model_column),
            options=create_options_from_parser(
                parser_namespace=args, options_class=DP3DDECalOptions
            ),
            h5parm=args.h5parm,
        )
    elif args.task == "gaincal":
        dp3_gaincal(
            ms.with_options(model_column=args.model_column),
            options=create_options_from_parser(
                parser_namespace=args, options_class=DP3GainCalOptions
            ),
            h5parm=args.h5parm,
        )
    elif args.task == "applycal":
        dp3_applycal(
            ms,
            h5parm=args.h5parm,
            options=create_options_from_parser(
                parser_namespace=args, options_class=DP3ApplyCalOptions
            ),
            output_column=args.output_column,
        )


if __name__ == "__main__":
    cli()
