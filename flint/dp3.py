"""Run DP3 steps against measurement sets through DP3's Python interface"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Annotated, Literal, Self, TypeAlias

from capn_crunch import add_options_to_parser, create_options_from_parser
from pydantic import (
    Field,
    FilePath,
    StringConstraints,
    field_validator,
    model_validator,
)

from flint.logging import logger
from flint.ms import check_column_in_ms
from flint.options import MS, BaseOptions
from flint.utils import get_packaged_resource_path

DDECalMode: TypeAlias = Literal[
    "scalar",
    "scalarphase",
    "scalaramplitude",
    "diagonal",
    "diagonalphase",
    "diagonalamplitude",
    "fulljones",
    "tec",
    "tec+delay",
    "tec+phase",
    "tec+phase+delay",
    "rotation",
    "rotation+diagonal",
    "faradayrotation",
    "leakage",
    "leakageamplitude",
]
GainCalType: TypeAlias = Literal[
    "scalar",
    "scalarphase",
    "scalaramplitude",
    "diagonal",
    "diagonalphase",
    "diagonalamplitude",
    "fulljones",
    "tec",
    "tec+phase",
]
CorrelationType: TypeAlias = Literal["auto", "cross"]
Interpolation: TypeAlias = Literal["nearest", "linear"]
# Characters that would break DP3's list syntax are not allowed
ParsetWord: TypeAlias = Annotated[str, StringConstraints(pattern=r"^[^\s,\[\]=]+$")]
ChannelRange: TypeAlias = Annotated[str, StringConstraints(pattern=r"^\d+(\.\.\d+)?$")]


class DP3PreFlaggerOptions(BaseOptions):
    """Options for the DP3 preflagger step. Unset selections are not used."""

    corrtype: CorrelationType | None = None
    """Flag only auto or cross correlations"""
    blmin: float | None = Field(default=None, ge=0)
    """Flag baselines shorter than this length in metres"""
    blmax: float | None = Field(default=None, ge=0)
    """Flag baselines longer than this length in metres"""
    uvmmin: float | None = Field(default=None, ge=0)
    """Flag data with a UV distance below this in metres"""
    uvmmax: float | None = Field(default=None, ge=0)
    """Flag data with a UV distance above this in metres"""
    chan: list[ChannelRange] | None = None
    """Channels or inclusive channel ranges to flag, e.g. ['0..7', '287']"""
    amplmin: float | None = Field(default=None, ge=0)
    """Flag data with an amplitude below this"""
    amplmax: float | None = Field(default=None, ge=0)
    """Flag data with an amplitude above this"""

    @field_validator("chan")
    @classmethod
    def check_channel_ranges(cls, chan: list[str] | None) -> list[str] | None:
        """Channel ranges must not run backwards."""
        for channels in chan or []:
            start, _, end = channels.partition("..")
            if end and int(start) > int(end):
                raise ValueError(f"Channel range {channels} runs backwards")
        return chan

    @model_validator(mode="after")
    def check_limits(self) -> Self:
        """Each lower limit must be below its upper limit."""
        for low, high in (
            ("blmin", "blmax"),
            ("uvmmin", "uvmmax"),
            ("amplmin", "amplmax"),
        ):
            low_value, high_value = getattr(self, low), getattr(self, high)
            if None not in (low_value, high_value) and low_value >= high_value:
                raise ValueError(f"{low}={low_value} must be below {high}={high_value}")
        return self


class DP3AOFlaggerOptions(BaseOptions):
    """Options for the DP3 aoflagger step"""

    strategy: FilePath | None = None
    """The aoflagger .lua strategy file. The packaged ASKAP strategy is used if unset."""
    memoryperc: int | None = Field(default=None, gt=0, le=100)
    """Percentage of system memory aoflagger may use"""
    timewindow: int | None = Field(default=None, ge=1)
    """Number of time slots flagged together"""

    @field_validator("strategy")
    @classmethod
    def check_strategy_is_lua(cls, strategy: Path | None) -> Path | None:
        """Strategies are lua scripts."""
        if strategy is not None and strategy.suffix != ".lua":
            raise ValueError(f"Strategy {strategy} is not a .lua file")
        return strategy


class DP3DDECalOptions(BaseOptions):
    """Options for the DP3 ddecal step"""

    mode: DDECalMode = "scalarphase"
    """What to solve for"""
    solint: int = Field(default=1, ge=0)
    """Number of time slots per solution. 0 uses the whole observation."""
    nchan: int = Field(default=0, ge=0)
    """Number of channels per solution. 0 uses the whole band."""
    maxiter: int | None = Field(default=None, ge=1)
    """Maximum number of solver iterations"""
    tolerance: float | None = Field(default=None, gt=0)
    """Convergence tolerance of the solver"""
    uvlambdamin: float | None = Field(default=None, ge=0)
    """Ignore baselines shorter than this many wavelengths"""
    propagatesolutions: bool | None = None
    """Start each solution interval from the previous solutions"""


class DP3GainCalOptions(BaseOptions):
    """Options for the DP3 gaincal step"""

    caltype: GainCalType = "scalarphase"
    """What to solve for"""
    solint: int = Field(default=1, ge=0)
    """Number of time slots per solution. 0 uses the whole observation."""
    nchan: int = Field(default=0, ge=0)
    """Number of channels per solution. 0 uses the whole band."""
    maxiter: int | None = Field(default=None, ge=1)
    """Maximum number of solver iterations"""
    tolerance: float | None = Field(default=None, gt=0)
    """Convergence tolerance of the solver"""
    uvlambdamin: float | None = Field(default=None, ge=0)
    """Ignore baselines shorter than this many wavelengths"""
    propagatesolutions: bool | None = None
    """Start each solution interval from the previous solutions"""


class DP3ApplyCalOptions(BaseOptions):
    """Options for the DP3 applycal step"""

    correction: list[ParsetWord] = Field(default=["phase000"], min_length=1)
    """Soltabs to apply in turn, e.g. ['phase000'] or ['rotationmeasure000']"""
    solset: ParsetWord | None = None
    """The solset holding the soltabs. DP3 picks one if unset."""
    interpolation: Interpolation | None = None
    """How solutions are interpolated in time and frequency"""
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


def check_columns(ms: MS, *columns: str | None) -> None:
    """Raise if any of the given columns is missing from the measurement set."""
    for column in columns:
        if column and not check_column_in_ms(ms, column=column):
            raise ValueError(f"Column {column} not found in {ms.path}")


def get_model_column(ms: MS) -> str:
    """The model column of a measurement set, which calibration needs."""
    if ms.model_column is None:
        raise ValueError(f"No model column set for {ms.path}")
    check_columns(ms, ms.column, ms.model_column)

    return ms.model_column


def dp3_flag(
    ms: MS | Path,
    aoflagger_options: DP3AOFlaggerOptions | None = None,
    preflagger_options: DP3PreFlaggerOptions | None = None,
) -> MS:
    """Flag a measurement set with aoflagger, after the preflagger if given.

    Args:
        ms (MS | Path): The measurement set, flagging its nominated column
        aoflagger_options (DP3AOFlaggerOptions | None, optional): Options for aoflagger. Defaults to the packaged ASKAP strategy.
        preflagger_options (DP3PreFlaggerOptions | None, optional): If set, run the preflagger first. Defaults to None.

    Returns:
        MS: The flagged measurement set
    """
    ms = MS.cast(ms)
    aoflagger_options = aoflagger_options or DP3AOFlaggerOptions()
    if aoflagger_options.strategy is None:
        aoflagger_options = aoflagger_options.with_options(
            strategy=get_packaged_resource_path(
                package="flint.data.aoflagger", filename="ASKAP.lua"
            )
        )

    check_columns(ms, ms.column)

    steps = ["aoflagger"]
    parset = options_to_parset("aoflagger", aoflagger_options)
    if preflagger_options:
        steps.insert(0, "preflagger")
        parset |= options_to_parset("preflagger", preflagger_options)

    run_dp3(create_dp3_parset(ms, steps) | parset)

    return ms


def dp3_ddecal(
    ms: MS | Path, options: DP3DDECalOptions | None = None, h5parm: Path | None = None
) -> MS:
    """Solve for gains against the model column with ddecal.

    Args:
        ms (MS | Path): The measurement set, with its model column set
        options (DP3DDECalOptions | None, optional): Options for ddecal. Defaults to None.
        h5parm (Path | None, optional): Where to write the solutions. Defaults to the MS path with a ``.ddecal.h5`` suffix.

    Raises:
        ValueError: Raised if the model column is not set or missing

    Returns:
        MS: The measurement set with ``h5parm`` set to the solutions
    """
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

    return ms.with_options(h5parm=h5parm)


def dp3_gaincal(
    ms: MS | Path,
    options: DP3GainCalOptions | None = None,
    h5parm: Path | None = None,
    output_column: str | None = None,
) -> MS:
    """Solve for gains against the model column with gaincal.

    Args:
        ms (MS | Path): The measurement set, with its model column set
        options (DP3GainCalOptions | None, optional): Options for gaincal. Defaults to None.
        h5parm (Path | None, optional): Where to write the solutions. Defaults to the MS path with a ``.gaincal.h5`` suffix.
        output_column (str | None, optional): If set, also apply the solutions in the same pass, writing to this column. Defaults to None.

    Raises:
        ValueError: Raised if the model column is not set or missing

    Returns:
        MS: The measurement set with ``h5parm`` set, and ``column`` set to ``output_column`` if given
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

    return ms.with_options(h5parm=h5parm, column=output_column or ms.column)


def dp3_applycal(
    ms: MS | Path,
    h5parm: Path | None = None,
    options: DP3ApplyCalOptions | None = None,
    output_column: str = "CORRECTED_DATA",
) -> MS:
    """Apply the solutions in an h5parm, writing the corrected data to ``output_column``.

    Args:
        ms (MS | Path): The measurement set to correct
        h5parm (Path | None, optional): The solutions to apply. Defaults to ``ms.h5parm``.
        options (DP3ApplyCalOptions | None, optional): Options for applycal. Defaults to None.
        output_column (str, optional): The column to write the corrected data to. Defaults to "CORRECTED_DATA".

    Raises:
        ValueError: Raised if no h5parm is given or set on the measurement set
        FileNotFoundError: Raised if the h5parm does not exist

    Returns:
        MS: The measurement set with ``column`` set to ``output_column`` and ``h5parm`` set
    """
    ms = MS.cast(ms)
    options = options or DP3ApplyCalOptions()
    h5parm = h5parm or ms.h5parm
    if h5parm is None:
        raise ValueError(f"No h5parm given or set for {ms.path}")
    if not h5parm.exists():
        raise FileNotFoundError(f"Solutions {h5parm} not found")
    check_columns(ms, ms.column)

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

    return ms.with_options(column=output_column, h5parm=h5parm)


def dp3_solve_apply(
    ms: MS | Path,
    options: DP3DDECalOptions | DP3GainCalOptions | None = None,
    h5parm: Path | None = None,
    output_column: str = "CORRECTED_DATA",
) -> MS:
    """Solve against the model column and apply the solutions to ``output_column``.

    Args:
        ms (MS | Path): The measurement set, with its model column set
        options (DP3DDECalOptions | DP3GainCalOptions | None, optional): Solve with ddecal or gaincal depending on the type. Defaults to ddecal.
        h5parm (Path | None, optional): Where to write the solutions. Defaults to a path next to the MS.
        output_column (str, optional): The column to write the corrected data to. Defaults to "CORRECTED_DATA".

    Returns:
        MS: The measurement set with ``column`` set to ``output_column`` and ``h5parm`` set
    """
    ms = MS.cast(ms)
    options = options or DP3DDECalOptions()

    if isinstance(options, DP3GainCalOptions):
        return dp3_gaincal(ms, options, h5parm=h5parm, output_column=output_column)

    # DDECal can not reliably hand its solutions to applycal in the same pass
    return dp3_applycal(
        dp3_ddecal(ms, options, h5parm=h5parm), output_column=output_column
    )


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
    match args.task:
        case "flag":
            dp3_flag(
                ms,
                aoflagger_options=create_options_from_parser(
                    parser_namespace=args, options_class=DP3AOFlaggerOptions
                ),
            )
        case "ddecal":
            dp3_ddecal(
                ms.with_options(model_column=args.model_column),
                options=create_options_from_parser(
                    parser_namespace=args, options_class=DP3DDECalOptions
                ),
                h5parm=args.h5parm,
            )
        case "gaincal":
            dp3_gaincal(
                ms.with_options(model_column=args.model_column),
                options=create_options_from_parser(
                    parser_namespace=args, options_class=DP3GainCalOptions
                ),
                h5parm=args.h5parm,
            )
        case "applycal":
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
