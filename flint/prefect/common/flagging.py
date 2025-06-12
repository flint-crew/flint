"""Prefect tasks around flagging"""

from __future__ import annotations

from prefect import task

from flint.flagging import jolly_uvw_flagger

task_jolly_uvw_flagger = task(jolly_uvw_flagger)
