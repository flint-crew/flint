"""Assorted bits and pieces to help with prefect interactions.

Not intended to contain any common task decorated functions
"""

from __future__ import annotations

from prefect import get_run_logger


# This function has been lifted from
# https://gist.github.com/anna-geller/0b9e6ecbde45c355af425cd5b97e303d
# like any good pirate why make when you can take
def enable_loguru_support() -> None:
    """Redirect loguru logging messages to the prefect run logger.
    This function should be called from within a Prefect task or flow before calling any module that uses loguru.
    This function can be safely called multiple times.

    Example Usage:

    >>> from prefect import flow
    >>> from loguru import logger
    >>> from prefect_utils import enable_loguru_support # import this function in your flow from your module
    >>> @flow()
    >>> def myflow():
    >>>     logger.info("This is hidden from the Prefect UI")
    >>>     enable_loguru_support()
    >>>     logger.info("This shows up in the Prefect UI")

    """
    # import here for distributed execution because loguru cannot be pickled.
    from loguru import logger  # pylint: disable=import-outside-toplevel

    run_logger = get_run_logger()
    logger.remove()
    log_format = "{name}:{function}:{line} - {message}"
    logger.add(
        run_logger.debug,
        filter=lambda record: record["level"].name == "DEBUG",
        level="TRACE",
        format=log_format,
    )
    logger.add(
        run_logger.warning,
        filter=lambda record: record["level"].name == "WARNING",
        level="TRACE",
        format=log_format,
    )
    logger.add(
        run_logger.error,
        filter=lambda record: record["level"].name == "ERROR",
        level="TRACE",
        format=log_format,
    )
    logger.add(
        run_logger.critical,
        filter=lambda record: record["level"].name == "CRITICAL",
        level="TRACE",
        format=log_format,
    )
    logger.add(
        run_logger.info,
        filter=lambda record: record["level"].name
        not in ["DEBUG", "WARNING", "ERROR", "CRITICAL"],
        level="TRACE",
        format=log_format,
    )
