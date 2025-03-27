from __future__ import annotations


class FlintException(Exception):
    """Base exception for Flint"""

    pass


class UnknownContainerException(FlintException):
    """An unknown container was requested"""

    pass


class NamingException(FlintException):
    """Raised when there are issues with naming"""

    pass


class NotSupportedError(FlintException):
    """Raised when something is not supported"""

    pass


class AttemptRerunException(FlintException):
    """Intended to be used in stream call back functions to
    capture strange errors and signify that they should be
    rerun. For instance, strange BANE deadlocking errors."""


class TimeLimitException(FlintException):
    """A function has taken too long to execute"""

    pass


class MSError(FlintException):
    """An error for MS related things"""

    pass


class FrequencyMismatchError(FlintException):
    """Raised when there are differences in frequencies"""


class PhaseOutlierFitError(FlintException):
    """Raised when the phase outlier fit routine fails."""

    pass


class GainCalError(FlintException):
    """Raised when it appears like the casa gaincal task fails."""

    pass


class CleanDivergenceError(FlintException):
    """Raised if it is detected that cleaning has diverged."""

    pass


class TarArchiveError(FlintException):
    """Raised it the flint tarball is not created successfullty"""
