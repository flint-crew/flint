"""Some utility functions around the creation of Prefect task funners.

For this work we will be using Dask backed workers to perform the compute
operations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from prefect_dask import DaskTaskRunner

from flint.utils import get_packaged_resource_path


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
) -> DaskTaskRunner:
    """Creates and returns a DaskTaskRunner configured to established a SLURMCluster instance
    to manage a set of dask-workers. The SLURMCluster is currently configured only for Galaxy.

    Keyword Args:
        cluster (Union[str,Path]): The cluster name that will be used to search for a cluster specification file.
                       This could be the name of a known cluster, or the name of a yaml file installed
                       among the `cluster_configs` directory of the aces module.

    Returns:
        DaskTaskRunner: A dask task runner capable of being used as a task_runner for a prefect flow
    """

    spec = get_cluster_spec(cluster)

    if extra_cluster_kwargs is not None:
        spec["cluster_kwargs"].update(extra_cluster_kwargs)

    task_runner = DaskTaskRunner(**spec)

    return task_runner
