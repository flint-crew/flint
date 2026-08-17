"""Assorted bits and pieces to help with prefect interactions.

Not intended to contain any common task decorated functions
"""

from __future__ import annotations

import logging

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
        filter=lambda record: (
            record["level"].name not in ["DEBUG", "WARNING", "ERROR", "CRITICAL"]
        ),
        level="TRACE",
        format=log_format,
    )


class _RMLiteRunLoggerHandler(logging.Handler):
    """Forwards rm-lite's stdlib log records to the active Prefect run logger."""

    _LEVEL_TO_METHOD = {
        logging.DEBUG: "debug",
        logging.INFO: "info",
        logging.WARNING: "warning",
        logging.ERROR: "error",
        logging.CRITICAL: "critical",
    }

    def emit(self, record: logging.LogRecord) -> None:
        method_name = self._LEVEL_TO_METHOD.get(record.levelno, "info")
        getattr(get_run_logger(), method_name)(self.format(record))


def enable_rmlite_logging_support() -> None:
    """Redirect rm-lite's stdlib logging (logger name 'rmtools-lite') to the
    Prefect run logger. This function should be called from within a Prefect
    task before calling any module that uses rm-lite. Safe to call multiple
    times.
    """
    rmlite_logger = logging.getLogger("rmtools-lite")
    for handler in list(rmlite_logger.handlers):
        if not isinstance(handler, _RMLiteRunLoggerHandler):
            rmlite_logger.removeHandler(handler)

    if not any(
        isinstance(handler, _RMLiteRunLoggerHandler)
        for handler in rmlite_logger.handlers
    ):
        rmlite_logger.addHandler(_RMLiteRunLoggerHandler())

    rmlite_logger.propagate = False
