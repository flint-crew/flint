"""Prefect task wrapper for RM-synthesis (``flint.rmsynth``).

Kept separate from ``flint.prefect.common.imaging`` (which every flow already
imports) so that the ``rm-lite`` optional dependency is only required by flows
that actually import this module.
"""

from __future__ import annotations

from prefect import task

from flint.rmsynth import rmsynth_and_write_products

task_rmsynth_and_write_products = task(rmsynth_and_write_products)
