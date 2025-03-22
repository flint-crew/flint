"""Items in and out the model visibility prediction using the `crystalball` python package."""

from __future__ import annotations

from flint.options import BaseOptions


class CrystalBallOptions(BaseOptions):
    """Options related to running crystal ball"""

    attempt_crystalball: bool = False
    """Attempt to predict the model visibilities using ``crystalball``"""
    crystallball_wsclean_pol_mode: list[str] = ["i"]
    """The polarisation of the wsclean model that was generated"""
    row_chunks: int = 0
    "Number of rows of input MS that are processed in a single chunk. If 0 it will be set automatically. Default is 0."
    model_chunks: int = 0
    "Number of sky model components that are processed in a single chunk. If 0 it will be set automatically. Default is 0."
