"""Utilities related to running commands in a singularity container"""

from __future__ import annotations

from pathlib import Path
from subprocess import CalledProcessError
from time import sleep
from typing import Callable, Collection, NamedTuple

from spython.main import Client as sclient

from flint.exceptions import AttemptRerunException
from flint.logging import logger
from flint.utils import get_job_info, log_job_environment


class ContainerCommandResults(NamedTuple):
    """Description of the command execution results"""

    attempts: int
    """Number of attempts the command took"""


def pull_container(container_directory: Path, uri: str, file_name: str) -> Path:
    """Download a singularity container from an appropriate ``uri``.

    Args:
        container_directory (Path): Where to store the output container
        uri (str): The reference to the container to pull, e.g. docker://profile/container:tag
        file_name (str): The output name of the container

    Returns:
        Path: Location of the new container
    """
    logger.info(f"Attempting to pull {uri=} into {container_directory=}")

    container_path, output = sclient.pull(
        image=uri, pull_folder=str(container_directory), name=file_name, stream=True
    )
    for line in output:
        # Remove the trailing new line character
        logger.info(line.rstrip())

    container_path = Path(container_path)

    assert container_path.exists(), f"{container_path=} does not exist, but should"

    return container_path


def run_singularity_command(
    image: Path,
    command: str,
    bind_dirs: Path | Collection[Path] | None = None,
    stream_callback_func: Callable | None = None,
    ignore_logging_output: bool = False,
    max_retries: int = 2,
) -> None:
    """Executes a command within the context of a nominated singularity
    container

    Args:
        image (Path): The singularity container image to use
        command (str): The command to execute
        bind_dirs (Optional[Union[Path,Collection[Path]]], optional): Specifies a Path, or list of Paths, to bind to in the container. Defaults to None.
        stream_callback_func (Optional[Callable], optional): Provide a function that is applied to each line of output text when singularity is running and `stream=True`. IF provide it should accept a single (string) parameter. If None, nothing happens. Defaultds to None.
        ignore_logging_output (bool, optional): If `True` output from the executed singularity command is not logged. Defaults to False.
        max_retries (int, optional): If a callback handler is specified which raised an `AttemptRerunException`, this signifies how many attempts should be made. Each rerun will recall `run_singularity_command` lowering this value. Defaults to 2.

    Raises:
        FileNotFoundError: Thrown when container image not found
        CalledProcessError: Thrown when the command into the container was not successful
    """
    if max_retries <= 0:
        raise ValueError("Too many retries")

    if not image.exists():
        raise FileNotFoundError(f"The singularity container {image} was not found. ")

    logger.info(f"Running {command} in {image}")

    job_info = log_job_environment()
    bind: None | list[str] = None
    if bind_dirs:
        logger.info("Preparing bind directories")
        if isinstance(bind_dirs, Path):
            bind_dirs = [bind_dirs]

        # Get only unique paths to avoid duplication in bindstring.
        # bind_str = ",".join(set([str(p.absolute().resolve()) for p in bind_dirs]))
        bind = (
            list(set([str(Path(p).absolute().resolve()) for p in bind_dirs]))
            if len(bind_dirs) > 0
            else None
        )

        logger.info(f"Constructed singularity bindings: {bind}")

    try:
        output = sclient.execute(
            image=image.resolve(strict=True).as_posix(),
            command=command.split(),
            bind=bind,
            return_result=True,
            quiet=False,
            stream=True,
            stream_type="both",
        )

        for line in output:
            if not ignore_logging_output:
                logger.info(line.rstrip())
            if stream_callback_func:
                stream_callback_func(line)

        # Sleep for a few moments. If the command created files (often they do), give the lustre a moment
        # to properly register them. You dirty sea dog.
        sleep(2.0)
    except AttemptRerunException as e:
        logger.info("A callback handler has raised an error. Attempting to rerun.")
        logger.info(f"{e=}")
        run_singularity_command(
            image=image,
            command=command,
            bind_dirs=bind_dirs,
            stream_callback_func=stream_callback_func,
            ignore_logging_output=ignore_logging_output,
            max_retries=max_retries - 1,
        )

    except CalledProcessError as e:
        logger.error(f"Failed to run command: {command}")
        logger.error(f"Stdout: {e.stdout}")
        logger.error(f"Stderr: {e.stderr}")
        logger.error(f"{e=}")
        job_info = get_job_info()
        if job_info:
            logger.error(f"{get_job_info()}")

        raise e


def singularity_wrapper(
    fn: Callable,
) -> Callable:
    """A decorator function to around another function that when executed
    returns a command to execute within a container. The returned function has
    the arguments as ``run_singularity_command``, and any unused keywords are
    passed to the wrapped function.

    Args:
        fn (Callable): The function that generates the command to execute

    Returns:
        Callable: Wrapper function
    """

    def wrapper(
        container: Path,
        bind_dirs: Path | Collection[Path] | None = None,
        stream_callback_func: Callable | None = None,
        ignore_logging_output: bool = False,
        **kwargs,
    ) -> str:
        """Function that can be used as a decorator on an input function. This function
        should generate and return a command that will be executed within the specified container.

        Args:
            fn (Callable): The function that generates a command to execute. All ``**kwargs`` are passed to this function
            container (Path): Path to the container that will be usede to execute the generated command
            bind_dirs (Optional[Union[Path,Collection[Path]]], optional): Specifies a Path, or list of Paths, to bind to in the container. Defaults to None.
            stream_callback_func (Optional[Callable], optional): Provide a function that is applied to each line of output text when singularity is running and `stream=True`. IF provide it should accept a single (string) parameter. If None, nothing happens. Defaults to None.
            ignore_logging_output (bool, optional): If `True` output from the executed singularity command is not logged. Defaults to False.

        Returns:
            str: The command that was executed
        """

        task_str = fn(**kwargs)

        assert isinstance(task_str, str), f"{task_str=}, but needs to be a string"

        logger.info(f"wrapper {task_str=}")
        logger.info(f"wrapper {bind_dirs=}")

        run_singularity_command(
            image=container,
            command=f"{task_str}",
            bind_dirs=bind_dirs,
            ignore_logging_output=ignore_logging_output,
            stream_callback_func=stream_callback_func,
        )

        return task_str

    return wrapper
