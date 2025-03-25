"""Very basic tests around the subtract model and cube work flow"""

from __future__ import annotations

from flint.prefect.flows.subtract_cube_pipeline import get_parser


def test_get_parser():
    """See the parser load successfully. Good test for imports and the like."""
    get_parser()
