"""Code to use AO calibrate s"""

from __future__ import annotations  # used to keep mypy/pylance happy in AOSolutions

import struct
from argparse import ArgumentParser
from pathlib import Path
from typing import (
    Any,
    Collection,
    Iterable,
    NamedTuple,
)

import matplotlib.pyplot as plt
import numpy as np

from flint.bptools.preflagger import (
    construct_jones_over_max_amp_flags,
    construct_mesh_ant_flags,
    flag_mean_residual_amplitude,
    flag_mean_xxyy_amplitude_ratio,
    flag_outlier_phase,
    flags_over_threshold,
)
from flint.bptools.smoother import (
    divide_bandpass_by_ref_ant_preserve_phase,
    smooth_bandpass_complex_gains,
)
from flint.exceptions import PhaseOutlierFitError
from flint.logging import logger
from flint.ms import consistent_ms, get_beam_from_ms
from flint.naming import get_aocalibrate_output_path
from flint.options import MS, BaseOptions
from flint.sclient import run_singularity_command
from flint.utils import create_directory


class CalibrateOptions(BaseOptions):
    """Structure used to represent options into the `calibrate` program

    These attributes have the same names as options into the `calibrate`
    command.
    """

    datacolumn: str
    """The name of the datacolumn that will be calibrates"""
    m: Path
    """The path to the model file used to calibtate"""
    minuv: float | None = None
    """The minimum distance in meters that is"""
    maxuv: float | None = None
    """The maximum distance in meters that is"""
    i: int | None = 100
    """The number of iterations that may be performed"""
    p: tuple[Path, Path] | None = None
    """Plot output names for the amplitude gain and phases"""


class CalibrateCommand(BaseOptions):
    """The AO Calibrate command and output path of the corresponding solutions file"""

    cmd: str
    """The calibrate command that will be executed
    """
    solution_path: Path
    """The output path of the solutions file
    """
    ms: MS
    """The measurement set to have solutions derived for"""
    model: Path
    """Path to the model that would be used to calibrate against"""
    preflagged: bool = False
    """Indicates whether the solution file has gone through preflagging routines. """


class ApplySolutions(BaseOptions):
    """The applysolutions command to execute"""

    cmd: str
    """The command that will be executed"""
    solution_path: Path
    """Location of the solutions file to apply"""
    ms: MS
    """The measurement set that will have the solutions applied to"""


# TODO: Rename the bandpass attribute?
class AOSolutions(NamedTuple):
    """Structure to load an AO-style solutions file"""

    path: Path
    """Path of the solutions file loaded"""
    nsol: int
    """Number of time solutions"""
    nant: int
    """Number of antenna in the solution file"""
    nchan: int
    """Number of channels in the solution file"""
    npol: int
    """Number of polarisations in the file"""
    bandpass: np.ndarray
    """Complex data representing the antenna Jones. Shape is (nsol, nant, nchan, npol)"""

    # TODO: Need tocorporate the start and end times into this header

    @classmethod
    def load(cls, path: Path) -> AOSolutions:
        """Load in an AO-stule solution file. See `load_solutions_file`, which is
        internally used.
        """
        return load_aosolutions_file(solutions_path=path)

    def save(self, output_path: Path) -> Path:
        """Save the instance of AOSolution to a standard aosolution binary file

        Args:
            output_path (Path): Location to write the file to

        Returns:
            Path: Location the file was written to
        """
        return save_aosolutions_file(aosolutions=self, output_path=output_path)

    def plot_solutions(self, ref_ant: int | None = 0) -> Iterable[Path]:
        """Plot the solutions of all antenna for the first time-interval
        in the aosolutions file. The XX and the YY will be plotted.

        Args:
            ref_ant (Optional[int], optional): Reference antenna to use. If None is specified there is no division by a reference antenna.  Defaults to 0.

        Returns:
            Iterable[Path]: Path to the phase and amplited plots created.
        """
        # TODO: Change call signature to pass straight through
        return plot_solutions(solutions=self, ref_ant=ref_ant)


def fill_between_flags(
    ax: plt.Axes,
    flags: np.ndarray,
    values: np.ndarray | None = None,
    direction: str = "x",
) -> None:
    """Plot vertical or horizontal lines where data are flagged.

    NOTE: This is pretty inefficient and not intended for regular use.

    Args:
        ax (plt.Axes): Axes object to plot lines on
        flags (np.ndarray): Flags to consider. If `True`, plot.
        values (Optional[np.ndarray], optional): The values to plot at. Useful if the position does not map to location. Defaults to None.
        direction (str, optional): If `x` use axvline, if `y` use axhline. Defaults to "x".
    """
    values = values if values else np.arange(len(flags))

    mask = np.argwhere(flags)
    plot_vals = values[mask]
    func = ax.axvline if direction == "x" else ax.axhline

    for v in plot_vals:
        func(v, color="black", alpha=0.3)


def plot_solutions(
    solutions: Path | AOSolutions, ref_ant: int | None = 0
) -> Collection[Path]:
    """Plot solutions for AO-style solutions

    Args:
        solutions (Path): Path to the solutions file
        ref_ant (Optional[int], optional): Reference antenna to use. If None is specified there is no division by a reference antenna.  Defaults to 0.

    Return:
        Collection[Path] -- The paths of the two plots createda
    """
    ao_sols = (
        AOSolutions.load(path=solutions) if isinstance(solutions, Path) else solutions
    )
    solutions_path = ao_sols.path
    logger.info(f"Plotting {solutions_path}")

    if ao_sols.nsol > 1:
        logger.warning(f"Found {ao_sols.nsol} intervals, plotting the first. ")
    plot_sol = 0  # The first time interval

    data = ao_sols.bandpass[plot_sol]
    if ref_ant is not None and ref_ant < 0:
        ref_ant = select_refant(bandpass=ao_sols.bandpass)
        logger.info(f"Overwriting reference antenna selection, using {ref_ant=}")

    if ref_ant is not None:
        data = divide_bandpass_by_ref_ant_preserve_phase(
            complex_gains=ao_sols.bandpass[plot_sol], ref_ant=ref_ant
        )

    amplitudes = np.abs(data)
    phases = np.angle(data, deg=True)
    channels = np.arange(ao_sols.nchan)

    ncolumns = 6
    nrows = ao_sols.nant // ncolumns
    if ncolumns * nrows < ao_sols.nant:
        nrows += 1
    logger.debug(f"Plotting {plot_sol=} with {ncolumns=} {nrows=}")

    fig_amp, axes_amp = plt.subplots(nrows, ncolumns, figsize=(15, 9))
    fig_ratio, axes_ratio = plt.subplots(nrows, ncolumns, figsize=(15, 9))
    fig_phase, axes_phase = plt.subplots(nrows, ncolumns, figsize=(15, 9))

    for y in range(nrows):
        for x in range(ncolumns):
            ant = y * nrows + x

            amps_xx = amplitudes[ant, :, 0]
            amps_yy = amplitudes[ant, :, 3]
            phase_xx = phases[ant, :, 0]
            phase_yy = phases[ant, :, 3]

            ratio = amps_xx / amps_yy

            if any([np.sum(~np.isfinite(amps)) == 0 for amps in (amps_xx, amps_yy)]):
                logger.warning(f"No valid data for {ant=}")
                continue

            max_amp_xx = (
                np.nanmax(amps_xx[np.isfinite(amps_xx)])
                if any(np.isfinite(amps_xx))
                else -1
            )
            max_amp_yy = (
                np.nanmax(amps_yy[np.isfinite(amps_yy)])
                if any(np.isfinite(amps_yy))
                else -1
            )
            min_amp_xx = (
                np.nanmin(amps_xx[np.isfinite(amps_xx)])
                if any(np.isfinite(amps_xx))
                else -1
            )
            min_amp_yy = (
                np.nanmin(amps_yy[np.isfinite(amps_yy)])
                if any(np.isfinite(amps_yy))
                else -1
            )
            ax_a, ax_p = axes_amp[y, x], axes_phase[y, x]
            ax_a = axes_amp[y, x]
            ax_r = axes_ratio[y, x]
            ax_a.plot(
                channels,
                amps_xx,
                marker=None,
                color="tab:blue",
                label="X" if y == 0 and x == 0 else None,
            )
            ax_a.plot(
                channels,
                amps_yy,
                marker=None,
                color="tab:red",
                label="Y" if y == 0 and x == 0 else None,
            )
            ax_r.plot(
                channels,
                ratio,
                marker=None,
                color="tab:green",
                label="X/Y" if y == 0 and x == 0 else None,
            )

            ax_a.set(
                ylim=(
                    min(min_amp_xx, min_amp_yy) * 0.9,
                    max(max_amp_xx, max_amp_yy) * 1.1,
                )
            )
            ax_a.axhline(1, color="black", linestyle="--", linewidth=0.5)
            ax_a.set_title(f"ant{ant:02d}", fontsize=8)
            fill_between_flags(ax_a, ~np.isfinite(amps_yy) | ~np.isfinite(amps_xx))

            ax_r.set(ylim=(0.8, 1.2))
            ax_r.axhline(1, color="black", linestyle="--", linewidth=0.5)
            ax_r.set_title(f"ant{ant:02d}", fontsize=8)
            fill_between_flags(ax_r, ~np.isfinite(amps_yy) | ~np.isfinite(amps_xx))

            ax_p.plot(
                channels,
                phase_xx,
                marker=None,
                color="tab:blue",
                label="X" if y == 0 and x == 0 else None,
            )
            ax_p.plot(
                channels,
                phase_yy,
                marker=None,
                color="tab:red",
                label="Y" if y == 0 and x == 0 else None,
            )
            ax_p.set(ylim=(-200, 200))
            ax_p.set_title(f"ak{ant:02d}", fontsize=8)
            fill_between_flags(ax_p, ~np.isfinite(phase_yy) | ~np.isfinite(phase_xx))

    fig_amp.legend()
    fig_phase.legend()
    fig_ratio.legend()

    fig_amp.suptitle(f"{ao_sols.path.name} - Amplitudes")
    fig_phase.suptitle(f"{ao_sols.path.name} - Phases")
    fig_ratio.suptitle(f"{ao_sols.path.name} - Amplitude Ratios")

    fig_amp.tight_layout()
    fig_ratio.tight_layout()
    fig_phase.tight_layout()

    out_amp = f"{solutions_path.with_suffix('.amplitude.png')!s}"
    logger.info(f"Saving {out_amp}.")
    fig_amp.savefig(out_amp)

    out_phase = f"{solutions_path.with_suffix('.phase.png')!s}"
    logger.info(f"Saving {out_phase}.")
    fig_phase.savefig(out_phase)

    out_ratio = f"{solutions_path.with_suffix('.ratio.png')!s}"
    logger.info(f"Saving {out_ratio}.")
    fig_ratio.savefig(out_ratio)

    return [Path(out_amp), Path(out_phase), Path(out_ratio)]


def save_aosolutions_file(aosolutions: AOSolutions, output_path: Path) -> Path:
    """Save a AOSolutions file to the ao-standard binary format.

    Args:
        aosolutions (ApplySolutions): Instance of the solutions to save
        output_path (Path): Output path to write the files to

    Returns:
        Path: Path the file was written to
    """

    header_format = "8s6I2d"
    header_intro = b"MWAOCAL\0"

    output_dir = output_path.parent
    if not output_dir.exists():
        logger.info(f"Creating {output_dir}.")
        output_dir.mkdir(parents=True)

    logger.info(f"Writing aosolutions to {output_path!s}.")
    with open(str(output_path), "wb") as out_file:
        out_file.write(
            struct.pack(
                header_format,
                header_intro,
                0,  # File type, only 0 mode available
                0,  # Structure type, 0 model available only
                aosolutions.nsol,
                aosolutions.nant,
                aosolutions.nchan,
                aosolutions.npol,
                0.0,  # time start, I don't believe these are used in most use cases
                0.0,  # time end, I don't believe these are used in most use cases
            )
        )
        aosolutions.bandpass.tofile(out_file)

    return output_path


def load_aosolutions_file(solutions_path: Path) -> AOSolutions:
    """Load in an AO-style solutions file

    Args:
        solutions_path (Path): The path of the solutions file to load

    Returns:
        AOSolutions: Structure container the deserialized solutions file
    """

    assert solutions_path.exists() and solutions_path.is_file(), (
        f"{solutions_path!s} either does not exist or is not a file. "
    )
    logger.info(f"Loading {solutions_path}")

    with open(solutions_path) as in_file:
        _junk = np.fromfile(in_file, dtype="<i4", count=2)

        header = np.fromfile(in_file, dtype="<i4", count=10)
        logger.info(f"Header extracted: {header=}")
        file_type = header[0]
        assert file_type == 0, f"Expected file_type of 0, found {file_type}"

        structure_type = header[1]
        assert file_type == 0, f"Expected structure_type of 0, found {structure_type}"

        nsol, nant, nchan, npol = header[2:6]
        sol_shape = (nsol, nant, nchan, npol)

        bandpass = np.fromfile(in_file, dtype="<c16", count=np.prod(sol_shape)).reshape(
            sol_shape
        )
        logger.info(f"Loaded solutions of shape {bandpass.shape}")

        return AOSolutions(
            path=solutions_path,
            nsol=nsol,
            nant=nant,
            nchan=nchan,
            npol=npol,
            bandpass=bandpass,
        )


def find_existing_solutions(
    bandpass_directory: Path,
    use_preflagged: bool = True,
    use_smoothed: bool = False,
) -> list[CalibrateCommand]:
    """Given a directory that contains a collection of bandpass measurement
    sets, attempt to identify a corresponding set of calibrate binary solution
    file.

    This search only supports the use of the default or known preflagger suffix.
    Limited support is provided to specify the expected calibrate suffix.

    These bandpass measurement sets should be processed already - which means just
    the B1936-638 field has been split out of the larger raw MS, flagged and
    calibrated. These steps are expected in order to get to the calibrate
    stage.

    Args:
        bandpass_directory (Path): Directory to search for split bandpass measurement sets
        use_preflagged (bool, optional): Add the pre-flag suffix when searching for solution files. This uses the expected suffix for preflagged solutions. Defaults to True.
        use_smoothed (bool, optional): Add the smoothed bandpass suffix when searching for solution files. This uses the expected suffix for smoothed solutions. Defaults to False.

    Returns:
        List[CalibrateCommand]: Collection of the calibrate command structures that are intended to be used to map the bandpass measurement sets to solution files.
    """
    logger.info(
        f"Searching {bandpass_directory} for existing measurement sets and solutions. "
    )

    bandpass_mss = list(bandpass_directory.glob("*ms"))
    logger.info(f"Found {len(bandpass_mss)} bandpass measurement sets")

    solution_paths = [
        get_aocalibrate_output_path(
            ms_path=bandpass_ms,
            include_preflagger=use_preflagged,
            include_smoother=use_smoothed,
        )
        for bandpass_ms in bandpass_mss
    ]

    # If not all the treasure could be found. At the moment this function will only
    # work if the bandpass solutions were made using the default values.
    assert all([solution_path.exists() for solution_path in solution_paths]), (
        f"Missing solution file constructed from scanning {bandpass_directory}. Check the directory. "
    )

    calibrate_cmds = [
        CalibrateCommand(
            cmd="None",
            ms=MS(ms),
            solution_path=solution_path,
            model=Path("None"),
            preflagged=True,
        )
        for (ms, solution_path) in zip(bandpass_mss, solution_paths)
    ]

    logger.info(f"Constructed {len(calibrate_cmds)} calibrate commands. ")

    return calibrate_cmds


def select_aosolution_for_ms(
    calibrate_cmds: list[CalibrateCommand], ms: MS | Path
) -> Path:
    """Attempt to select an AO-style solution file for a measurement
    set. This can be expanded to include a number of criteria, but
    at present it only searches for a matching beam number between
    the input set of CalibrationCommands and the input MS.

    Args:
        calibrate_cmds (List[CalibrateCommand]): Set of calibration commands, which includes the solution file path and the corresponding MS, as attributes.
        ms (Union[MS, Path]): The measurement sett that needs a solutions file.

    Raises:
        ValueError: Raised when not matching AO-solution file found.

    Returns:
        Path: Path to solution file to apply.
    """
    ms = MS.cast(ms)
    ms_beam = ms.beam if ms.beam is not None else get_beam_from_ms(ms=ms)

    logger.info(f"Will select a solution for {ms.path!s}, {ms_beam=}.")
    logger.info(f"{len(calibrate_cmds)} potential solutions to consider. ")

    for calibrate_cmd in calibrate_cmds:
        logger.info(f"Considering {calibrate_cmd.solution_path!s}.")
        if consistent_ms(ms1=ms, ms2=calibrate_cmd.ms):
            sol_file = calibrate_cmd.solution_path
            break
    else:
        raise ValueError(
            f"No solution file found for {ms.path!s} from {[c.ms.path for c in calibrate_cmds]} found. "
        )

    logger.info(f"Have selected {sol_file!s} for {ms.path!s}. ")
    return sol_file


def calibrate_options_to_command(
    calibrate_options: CalibrateOptions, ms_path: Path, solutions_path: Path
) -> str:
    """Generate a `calibrate` command given an input option set

    Args:
        calibrate_options (CalibrateOptions): The set of `calibrate` options to use
        ms (Path): Path to the measurement set that will be calibrated
        solutions_path (Path): Output path of the solutions file

    Returns:
        str: The command string to execute
    """
    cmd = "calibrate "

    unknowns: list[tuple[Any, Any]] = []

    for key, value in calibrate_options._asdict().items():
        if value is None:
            continue
        elif isinstance(value, (str, Path, int, float)):
            cmd += f"-{key} {value!s} "
        elif isinstance(value, (tuple, list)):
            values = " ".join([str(v) for v in value])
            cmd += f"-{key} {values} "
        else:
            unknowns.append((key, value))

    assert len(unknowns) == 0, (
        f"Unknown types when generating calibrate command: {unknowns}"
    )

    cmd += f"{ms_path!s} {solutions_path!s}"

    return cmd


def create_calibrate_cmd(
    ms: Path | MS,
    calibrate_model: Path,
    solution_path: Path | None = None,
    container: Path | None = None,
    update_calibrate_options: dict[str, Any] | None = None,
    calibrate_data_column: str | None = None,
) -> CalibrateCommand:
    """Generate a typical ao calibrate command. Any extra keyword arguments
    are passed through as additional options to the `calibrate` program.

    Args:
        ms (Union[Path,MS]): The measurement set to calibrate. There needs to be a nominated data_column.
        calibrate_model (Path): Path to a generated calibrate sky-model
        solution_path (Path, optional): The output path of the calibrate solutions file. If None, a default suffix of "calibrate.bin" is used. Defaults to None.
        container (Optional[Path], optional): If a path to a container is supplied the calibrate command is executed immediately. Defaults to None.
        update_calibrate_options (Optional[Dict[str, Any]], optional): Additional options to update the generated CalibrateOptions with. Keys should be attributes of CalibrationOptions. Defaults to None.
        calibrate_data_column(Optional[str], optional): The name of the column to calibrate, overwriting the nominated column set in the MS. If None, the MS.column attribute is used. Defaults to None.

    Raises:
        FileNotFoundError: Raised when calibrate_model can not be found.

    Returns:
        CalibrateCommand: The calibrate command to execute and output solution file
    """
    ms = MS.cast(ms)

    column = ms.column
    if calibrate_data_column:
        logger.info(
            f"Overwriting column to calibrate from {ms.column=} to {calibrate_data_column=}"
        )
        column = calibrate_data_column

    assert column is not None, f"{ms} does not have a nominated data_column"

    logger.info(f"Creating calibrate command for {ms.path}")
    logger.info(f"Will calibrate data column {column}")

    # This is a typical calibrate command.
    # calibrate -minuv 100 -i 50 -datacolumn DATA
    #        -m 2022-04-14_100122_0.calibrate.txt
    #        2022-04-14_100122_0.ms 2022-04-14_100122_0.aocalibrate.bin

    if not calibrate_model.exists():
        raise FileNotFoundError(f"Calibrate model {calibrate_model} not found. ")

    if solution_path is None:
        solution_path = get_aocalibrate_output_path(
            ms_path=ms.path, include_preflagger=False, include_smoother=False
        )

    calibrate_options = CalibrateOptions(
        datacolumn=column, m=calibrate_model, minuv=600
    )
    if update_calibrate_options:
        calibrate_options = calibrate_options.with_options(**update_calibrate_options)

    cmd = calibrate_options_to_command(
        calibrate_options=calibrate_options,
        ms_path=ms.path,
        solutions_path=solution_path,
    )
    logger.debug(f"Constructed calibrate command is {cmd=}")

    calibrate_cmd = CalibrateCommand(
        cmd=cmd, solution_path=solution_path, ms=ms, model=calibrate_model
    )

    if container is not None:
        run_calibrate(calibrate_cmd=calibrate_cmd, container=container)

    return calibrate_cmd


def create_apply_solutions_cmd(
    ms: MS,
    solutions_file: Path,
    output_column: str | None = None,
    container: Path | None = None,
) -> ApplySolutions:
    """Construct the command to apply calibration solutions to a MS
    using an AO calibrate style solutions file.

    The `applysolutions` program does not appear to have the ability to set
    a desured output column name. If the `output_column` specified matches
    the nominated column in `ms`, then `applysolutions` will simply overwrite
    the column with updated data. Otherwise, a `CORRECTED_DATA` column is produced.

    NOTE: Care to be taken when the nominated column is `CORRECTED_DATA`.

    Args:
        ms (MS): Measurement set to have solutions applied to
        solutions_file (Path): Path to the solutions file to apply
        output_column (Optional[str], optional): The desired output column name. See notes above. Defaults to None.
        container (Optional[Path], optional): If a path to a container is supplied the calibrate command is executed immediately. Defaults to None.

    Returns:
        ApplySolutions: Description of applysolutions command, solutions file path and updated MS
    """
    # extract the ms property, if required
    ms = MS.cast(ms)

    assert ms.path.exists(), f"The measurement set {ms} was not found. "
    assert ms.column is not None, f"{ms} does not have a nominated data_column. "
    assert solutions_file.exists(), (
        f"The solutions file {solutions_file} does not exists. "
    )

    input_column = ms.column
    copy_mode = "-nocopy" if input_column == output_column else "-copy"

    logger.info(f"Setting {copy_mode=}.")

    if copy_mode == "-copy":
        output_column = "CORRECTED_DATA"

    cmd = (
        f"applysolutions "
        f"-datacolumn {input_column} "
        f"{copy_mode} "
        f"{ms.path!s} "
        f"{solutions_file!s} "
    )

    logger.info(f"Constructed {cmd=}")

    apply_solutions = ApplySolutions(
        cmd=cmd, solution_path=solutions_file, ms=ms.with_options(column=output_column)
    )

    if container is not None:
        run_apply_solutions(apply_solutions_cmd=apply_solutions, container=container)

    # TODO: If outputcolumn is not CORRECTED_DATA then it should be renamed
    # applysolutions always calls it CORRECTED_DATA

    return apply_solutions


def run_calibrate(calibrate_cmd: CalibrateCommand, container: Path) -> None:
    """Execute a calibrate command within a singularity container

    Args:
        calibrate_cmd (CalibrateCommand): The constructed calibrate command
        container (Path): Location of the container
    """

    assert container.exists(), f"The calibrate container {container} does not exist. "
    assert calibrate_cmd.ms is not None, (
        "When calibrating the 'ms' field attribute must be defined. "
    )

    run_singularity_command(
        image=container,
        command=calibrate_cmd.cmd,
        bind_dirs=[
            calibrate_cmd.solution_path.parent,
            calibrate_cmd.ms.path.parent,
            calibrate_cmd.model.parent,
        ],
    )


def run_apply_solutions(apply_solutions_cmd: ApplySolutions, container: Path) -> None:
    """Will execute the applysolutions command inside the specified singularity
    container.

    Args:
        apply_solutions_cmd (ApplySolutions): The constructed applysolutions command
        container (Path): Location of the existing solutions file
    """

    assert container.exists(), (
        f"The applysolutions container {container} does not exist. "
    )
    assert apply_solutions_cmd.ms.path.exists(), (
        f"The measurement set {apply_solutions_cmd.ms} was not found. "
    )

    run_singularity_command(
        image=container,
        command=apply_solutions_cmd.cmd,
        bind_dirs=[
            apply_solutions_cmd.solution_path.parent.absolute(),
            apply_solutions_cmd.ms.path.parent.absolute(),
        ],
    )


def calibrate_apply_ms(
    ms_path: Path, model_path: Path, container: Path, data_column: str = "DATA"
) -> ApplySolutions:
    """Will create and run a calibration command using AO calibrator, and then apply these solutions.

    Args:
        ms_path (Path): The measurement set that will be calibrated
        model_path (Path): The model file containing sources to calibrate against
        container (Path): Container that has the AO calibtate and applysolutions file.
        data_column (str, optional): The name of the column containing the data to calibrate. Defaults to "DATA".

    Returns:
        Applysolutions: The command, solution binary path and new measurement set structure
    """
    ms = MS(path=ms_path, column=data_column)

    logger.info(f"Will be attempting to calibrate {ms}")

    calibrate_cmd = create_calibrate_cmd(ms=ms, calibrate_model=model_path)

    run_calibrate(calibrate_cmd=calibrate_cmd, container=container.absolute())

    flagged_solutions = flag_aosolutions(
        solutions_path=calibrate_cmd.solution_path,
        ref_ant=0,
        plot_dir=Path(ms_path.parent) / Path("preflagger"),
    )

    apply_solutions_cmd = create_apply_solutions_cmd(
        ms=ms, solutions_file=flagged_solutions.path
    )

    run_apply_solutions(
        apply_solutions_cmd=apply_solutions_cmd, container=container.absolute()
    )

    return apply_solutions_cmd


def apply_solutions_to_ms(
    ms: Path | MS,
    solutions_path: Path,
    container: Path,
    data_column: str = "DATA",
) -> ApplySolutions:
    ms = ms if isinstance(ms, MS) else MS(path=ms, column=data_column)
    logger.info(f"Will attempt to apply {solutions_path!s} to {ms.path!s}.")

    apply_solutions_cmd = create_apply_solutions_cmd(
        ms=ms, solutions_file=solutions_path
    )

    run_apply_solutions(
        apply_solutions_cmd=apply_solutions_cmd, container=container.absolute()
    )

    return apply_solutions_cmd


def select_refant(bandpass: np.ndarray) -> int:
    """Attempt to select an optimal reference antenna. This works in
    a fairly simple way, and simply selects the antenna which is select
    based purely on the number of valid/unflagged solutions in the
    bandpass aosolutions file.

    Args:
        bandpass (np.ndarray): The aosolutions file that has been
        solved for

    Returns:
        int: The index of the reference antenna that should be used.
    """

    assert len(bandpass.shape) == 4, (
        f"Expected a bandpass of shape (times, ant, channels, pol), received {bandpass.shape=}"
    )

    # create the mask of valid solutions
    mask = np.isfinite(bandpass)
    # Sum_mask will be a shape of length 2 (time, ants)
    sum_mask = np.sum(mask, axis=(2, 3))

    # The refant will be the one with the highest number
    max_ant = np.argmax(sum_mask, keepdims=True)

    return max_ant[0][0]


class FlaggedAOSolution(NamedTuple):
    """Hold the final set of flagged solutions and generated plots"""

    path: Path
    """Path to the final set of flagged solutions"""
    plots: Collection[Path]
    """Contains paths to the plots generated throughout the flagging and smoothing procedure"""
    bandpass: np.ndarray
    """The bandpass solutions after flagging, as saved in the solutions file"""


# TODO: These options are too much and should be placed
# into a BaseOptions


def flag_aosolutions(
    solutions_path: Path,
    ref_ant: int = -1,
    flag_cut: float = 3,
    plot_dir: Path | None = None,
    out_solutions_path: Path | None = None,
    smooth_solutions: bool = False,
    plot_solutions_throughout: bool = True,
    smooth_window_size: int = 16,
    smooth_polynomial_order: int = 4,
    mean_ant_tolerance: float = 0.2,
    mesh_ant_flags: bool = False,
    max_gain_amplitude: float | None = None,
) -> FlaggedAOSolution:
    """Will open a previously solved ao-calibrate solutions file and flag additional channels and antennae.

    There are a number of distinct operations applied to the data, which are
    presented in order they are applied.

    If `mesh_ant_flags` is `True`, channels flagged from on channel on a single
    antenna will be applied to all (unless an antenna is completely flagged).
    This happens before any other operation,.

    If `max_gain_amplitude` is not `None` than any Jones with an element
    whose amplitude is above the set value will be flagged.

    Next, an attempt is made to search for channels where the the phase of the
    gain solution are outliers. The phase over frequency is first unwrapped (delay solved for) before the flagging
    statistics are computed.

    If an antenna is over 80% flagged then it is completely removed.

    A low order polynomial (typically order 5) is fit to the amplitudes of the
    Gx and Gy, and if the residuals are sufficiently high then the antenna will
    be flagged.

    If the mean ratio of the Gx and Gy amplitudes for an antenna are higher
    then `mean_ant_tolerance` then the antenna will be flagged.

    Keywords that with the `smooth` prefix are passed to the `smooth_bandpass_complex_gains` function.

    Args:
        solutions_path (Path): Location of the solutions file to examine and flag.
        ref_ant (int, optional): Reference antenna to use, which is important when searching for phase-outliers and to smooth the bandpass. If ref_ant < 0, then an optimal one is selected. Defaults to -1.
        flag_cut (float, optional): Significance of a phase-outlier from the mean (or median) before it should be flagged. Defaults to 3.
        plot_dir (Optional[Path], optional): Where diagnostic flagging plots should be written. If None, no plots will be produced. Defaults to None.
        out_solutions_path (Optional[Path], optional): The output path of the flagged solutions file. If None, the solutions_path provided is used. Defaults to None.
        smooth_solutions (blool, optional): Smooth the complex gain solutions after flaggined. Defaults to False.
        plot_solutions_throughout (bool, Optional): If True, the solutions will be plotted at different stages of processing. Defaults to True.
        smooth_window_size (int, optional): The size of the window function of the savgol filter. Passed directly to savgol. Defaults to 16.
        smooth_polynomial_order (int, optional): The order of the polynomial of the savgol filter. Passed directly to savgol. Defaults to 4.
        mean_ant_tolerance (float, optional): Tolerance of the mean x/y antenna gain ratio test before the antenna is flagged. Defaults to 0.2.
        mesh_ant_flags (bool, optional): If True, a channel is flagged across all antenna if it is flagged for any antenna. Performed before other flagging operations. Defaults to False.
        max_gain_amplitude (Optional[float], optional): If not None, flag the Jones if an antenna has a amplitude gain above this value. Defaults to 10.

    Returns:
        FlaggedAOSolution: Path to the updated solutions file, intermediate solution files and plots along the way
    """
    # TODO: This should be broken down into separate stages. Way too large of a function.
    # TODO: This pirate needs to cull some of this logic out, likely not needed
    # and dead

    solutions = AOSolutions.load(path=solutions_path)
    title = solutions_path.name

    pols = {0: "XX", 1: "XY", 2: "YX", 3: "YY"}

    if plot_dir:
        create_directory(directory=plot_dir)

    # Note that although the solutions variable (an instance of AOSolutions) is immutable,
    # which includes the reference to the numpy array, the _actual_ numpy array is! So,
    # copying the bandpass below is as we are updating the actual array, which will be
    # written back as a new file later.
    bandpass = solutions.bandpass
    logger.info(f"Loaded bandpass, shape is {bandpass.shape}")

    if ref_ant < 0:
        ref_ant = select_refant(bandpass=solutions.bandpass)
        logger.info(f"Overwriting reference antenna selection, using {ref_ant=}")

    plots: list[Path] = []

    if plot_solutions_throughout:
        output_plots = plot_solutions(solutions=solutions_path, ref_ant=ref_ant)
        plots.extend(output_plots)

    if mesh_ant_flags:
        logger.info("Combining antenna flags")
        mask = np.zeros_like(bandpass, dtype=bool)

        for time in range(solutions.nsol):
            mask[time] = construct_mesh_ant_flags(mask=~np.isfinite(bandpass[time]))

        bandpass[mask] = np.nan

    if max_gain_amplitude:
        mask = construct_jones_over_max_amp_flags(
            complex_gains=bandpass, max_amplitude=max_gain_amplitude
        )
        bandpass[mask] = np.nan

    for time in range(solutions.nsol):
        ref_bandpass = divide_bandpass_by_ref_ant_preserve_phase(
            complex_gains=bandpass[time], ref_ant=ref_ant
        )
        for pol in (0, 3):
            logger.info(f"Processing {pols[pol]} polarisation")

            for ant in range(solutions.nant):
                if ant == ref_ant:
                    logger.info(f"Skipping reference antenna = ant{ref_ant:02}")
                    continue

                ant_gains = ref_bandpass[ant, :, pol]
                plot_title = f"{title} - ant{ant:02d} - {pols[pol]}"
                output_path = (
                    plot_dir / f"{title}.ant{ant:02d}.{pols[pol]}.png"
                    if plot_dir is not None
                    else None
                )

                if np.sum(np.isfinite(ant_gains)) == 0:
                    logger.info(f"Not valid data found for ant{ant:0d} {pols[pol]}")
                    continue

                try:
                    phase_outlier_result = flag_outlier_phase(
                        complex_gains=ant_gains,
                        flag_cut=flag_cut,
                        plot_title=plot_title,
                        plot_path=output_path,
                    )
                    bandpass[time, ant, phase_outlier_result.outlier_mask, :] = np.nan
                except PhaseOutlierFitError:
                    # This is raised if the fit failed to converge, or some other nasty.
                    bandpass[time, ant, :, :] = np.nan

    for time in range(solutions.nsol):
        for pol in (0, 3):
            for ant in range(solutions.nant):
                # Flag all solutions for this (ant,pol) if more than 80% are flagged
                if flags_over_threshold(
                    flags=~np.isfinite(bandpass[time, ant, :, pol]),
                    thresh=0.8,
                    ant_idx=ant,
                ):
                    logger.info(
                        f"Flagging all solutions across  ant{ant:02d}, too many flagged channels."
                    )
                    bandpass[time, ant, :, :] = np.nan

                complex_gains = bandpass[time, ant, :, pol]
                if flag_mean_residual_amplitude(complex_gains=complex_gains):
                    logger.info(
                        f"Flagging all solutions for ant{ant:02d}, mean residual amplitudes high"
                    )
                    bandpass[time, ant, :, :] = np.nan

                flagged = ~np.isfinite(bandpass[time, ant, :, pol])
                logger.info(
                    f"{ant=:02d}, pol={pols[pol]}, flagged {np.sum(flagged) / ant_gains.shape[0] * 100.0:.2f}%"
                )

    for time in range(solutions.nsol):
        bandpass_phased_referenced = divide_bandpass_by_ref_ant_preserve_phase(
            complex_gains=bandpass[time], ref_ant=ref_ant
        )
        # This loop will flag based on stats across different polarisations
        for ant in range(solutions.nant):
            ant_gains = bandpass_phased_referenced[ant]
            if flag_mean_xxyy_amplitude_ratio(
                xx_complex_gains=ant_gains[:, 0],
                yy_complex_gains=ant_gains[:, 3],
                tolerance=mean_ant_tolerance,
            ):
                logger.info(f"{ant=} failed mean amplitude gain test. Flagging {ant=}.")
                bandpass[time, ant, :, :] = np.nan

    # To this point operations carried out to the bandpass were to the mutable array reference
    # so there is no need to create a new solutions instance
    out_solutions_path = get_aocalibrate_output_path(
        ms_path=solutions_path, include_preflagger=True, include_smoother=False
    )
    solutions.save(output_path=out_solutions_path)
    if plot_solutions_throughout:
        output_plots = plot_solutions(solutions=out_solutions_path, ref_ant=ref_ant)
        plots.extend(output_plots)

    if smooth_solutions:
        logger.info("Smoothing the bandpass solutions. ")
        for time in range(solutions.nsol):
            complex_gains = divide_bandpass_by_ref_ant_preserve_phase(
                complex_gains=bandpass[time], ref_ant=ref_ant
            )
            bandpass[time] = smooth_bandpass_complex_gains(
                complex_gains=complex_gains,
                window_size=smooth_window_size,
                polynomial_order=smooth_polynomial_order,
            )

        out_solutions_path = get_aocalibrate_output_path(
            ms_path=solutions_path, include_preflagger=True, include_smoother=True
        )
        solutions.save(output_path=out_solutions_path)
        if plot_solutions_throughout:
            output_plots = plot_solutions(solutions=out_solutions_path, ref_ant=None)
            plots.extend(output_plots)

    total_flagged = np.sum(~np.isfinite(bandpass)) / np.prod(bandpass.shape)
    if total_flagged > 0.8:
        msg = (
            f"{total_flagged * 100.0:.2f}% of {(solutions_path)!s} is flagged after running the preflagger. "
            "That is over 90%. "
            f"This surely can not be correct. Likely something has gone very wrong. "
        )
        logger.critical(msg)
        raise ValueError(msg)

    flagged_aosolutions = FlaggedAOSolution(
        path=out_solutions_path, plots=tuple(plots), bandpass=bandpass
    )

    return flagged_aosolutions


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="Run calibrate and apply the solutions given a measurement set and sky-model."
    )

    subparsers = parser.add_subparsers(
        dest="mode", help="AO Calibrate related operations"
    )
    calibrate_parser = subparsers.add_parser(
        "calibrate",
        help="Calibrate a MS using a text-based sky-model using AO calibrate",
    )

    calibrate_parser.add_argument(
        "ms",
        type=Path,
        help="The measurement set to calibrate and apply solutions to. ",
    )
    calibrate_parser.add_argument(
        "aoskymodel",
        type=Path,
        help="The AO-style sky-model file to use when calibrating. ",
    )
    calibrate_parser.add_argument(
        "--calibrate-container",
        type=Path,
        default="./calibrate.sif",
        help="The container containing calibrate and applysolutions. ",
    )
    calibrate_parser.add_argument(
        "--data-column", type=str, default="DATA", help="The column to calibrate"
    )

    apply_parser = subparsers.add_parser(
        "apply",
        help="Apply an existing AO-style solutions binary to a measurement set. ",
    )

    apply_parser.add_argument(
        "ms", type=Path, help="Path to the measurement set to apply the solutions to. "
    )
    apply_parser.add_argument(
        "aosolutions", type=Path, help="Path to the AO-style binary solutions file. "
    )
    apply_parser.add_argument(
        "--calibrate-container",
        type=Path,
        default="./calibrate.sif",
        help="The container containing calibrate and applysolutions. ",
    )
    apply_parser.add_argument(
        "--data-column", type=str, default="DATA", help="The column to calibrate"
    )

    flag_sols_parser = subparsers.add_parser(
        "flag",
        help="Attempt to flag the bandpass solutions in an ao-style binary solutions file",
    )

    flag_sols_parser.add_argument(
        "aosolutions", type=Path, help="Path to the solution file to inspect and flag"
    )
    flag_sols_parser.add_argument(
        "--flag-cut",
        type=float,
        default=3.0,
        help="The significance level that an outlier phase has to be before being flagged",
    )
    flag_sols_parser.add_argument(
        "--plot-dir",
        type=Path,
        default=None,
        help="Directory to write diagnostic plots to. If unset no plots will be created. ",
    )

    return parser


def cli() -> None:
    import logging

    parser = get_parser()

    args = parser.parse_args()

    logger.setLevel(logging.DEBUG)

    if args.mode == "calibrate":
        calibrate_apply_ms(
            ms_path=args.ms,
            model_path=args.aoskymodel,
            container=args.calibrate_container,
            data_column=args.data_column,
        )
    elif args.mode == "apply":
        apply_solutions_to_ms(
            ms=args.ms,
            solutions_path=args.aosolutions,
            container=args.calibrate_container,
            data_column=args.data_column,
        )
    elif args.mode == "flag":
        flag_aosolutions(
            solutions_path=args.aosolutions,
            flag_cut=args.flag_cut,
            plot_dir=args.plot_dir,
        )


if __name__ == "__main__":
    cli()
