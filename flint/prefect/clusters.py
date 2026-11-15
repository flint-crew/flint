"""Some utility functions around the creation of Prefect task funners.

For this work we will be using Dask backed workers to perform the compute
operations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from prefect.context import FlowRunContext
from prefect_dask import DaskTaskRunner

from flint.logging import logger
from flint.utils import get_packaged_resource_path


class FlintDaskTaskRunner(DaskTaskRunner):
    """A DaskTaskRunner that can save the dask performance report of each flow run.

    The report is written to ``performance_report_dir``, named after the flow run.
    """

    def __init__(
        self,
        *args: Any,
        performance_report_dir: str | Path | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.performance_report_dir = (
            Path(performance_report_dir) if performance_report_dir else None
        )

    def duplicate(self) -> FlintDaskTaskRunner:
        runner = super().duplicate()
        runner.performance_report_dir = self.performance_report_dir
        return runner

    @property
    def client(self):
        # The client is made on the first submit, inside the flow run, which is
        # the only point the flow run (and so a unique report name) is known.
        if self._client is None and self.performance_report_dir is not None:
            flow_run_context = FlowRunContext.get()
            name = flow_run_context.flow_run.name if flow_run_context else "flow"
            self.performance_report_dir.mkdir(parents=True, exist_ok=True)
            self.performance_report_path = str(
                self.performance_report_dir / f"dask-performance-report-{name}.html"
            )
            logger.info(
                f"Saving the dask performance report to {self.performance_report_path}"
            )
        return super().client


def get_cluster_spec(cluster: str | Path) -> dict[Any, Any]:
    """
    Given a cluster name, obtain the appropriate SLURM configuration
    file appropriate for use with SLURMCluster.

    This cluster spec is expected to be consistent with the cluster_class
    and cluster_kwargs parameters that are used by dask_jobqueue based
    specifications.

    Args:
        cluster (Union[str,Path]): Name of cluster or path to a configuration to look up for processing

    Raises:
        ValueError: Raised when cluster is not in KNOWN_CLUSTERS and has not corresponding YAML file.

    Returns:
        dict[Any, Any]: Dictionary of know options/parameters for dask_jobqueue.SLURMCluster
    """

    yaml_file = None

    if Path(cluster).exists():
        yaml_file = cluster
    else:
        yaml_file = get_packaged_resource_path(
            package="flint.data.cluster_configs", filename=f"{cluster}.yaml"
        )

    if yaml_file is None or not Path(yaml_file).exists():
        raise ValueError(
            f"{cluster=} is not known, or its YAML file could not be loaded."
        )

    with open(yaml_file) as in_file:
        spec = yaml.load(in_file, Loader=yaml.Loader)

    return spec


def get_dask_runner(
    cluster: str | Path = "galaxy_small",
    extra_cluster_kwargs: dict[str, Any] | None = None,
) -> FlintDaskTaskRunner:
    """Creates and returns a DaskTaskRunner configured to established a SLURMCluster instance
    to manage a set of dask-workers. The SLURMCluster is currently configured only for Galaxy.

    Keyword Args:
        cluster (Union[str,Path]): The cluster name that will be used to search for a cluster specification file.
                       This could be the name of a known cluster, or the name of a yaml file installed
                       among the `cluster_configs` directory of the aces module.
                       Setting ``performance_report_dir`` in it saves the dask performance report of each flow run there.

    Returns:
        FlintDaskTaskRunner: A dask task runner capable of being used as a task_runner for a prefect flow
    """

    spec = get_cluster_spec(cluster)

    if extra_cluster_kwargs is not None:
        spec["cluster_kwargs"].update(extra_cluster_kwargs)

    task_runner = FlintDaskTaskRunner(**spec)

    return task_runner
