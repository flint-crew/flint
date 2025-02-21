"""Simple tests for some prefect cluster helper functions"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from prefect_dask import DaskTaskRunner

from flint.prefect.clusters import get_cluster_spec, get_dask_runner


def test_example_yaml_from_file(tmpdir):
    """Place a Yaml file in a directory, and load it"""

    yaml_dir = Path(tmpdir) / "some_file1"
    yaml_dir.mkdir(parents=True, exist_ok=True)

    yaml_cluster = yaml_dir / "test.yaml"
    dump = dict(pirate="jack")

    with open(yaml_cluster, "w") as out_file:
        yaml.dump(dump, out_file)

    in_data = get_cluster_spec(cluster=yaml_cluster)

    assert "pirate" in in_data
    assert in_data["pirate"] == "jack"


def test_get_dask_task_runner():
    """Make sure we can fire up a dask task runner. This uses an example
    slurm cluster"""
    dask_task_runner = get_dask_runner(cluster="example_slurm")
    assert isinstance(dask_task_runner, DaskTaskRunner)

    dask_task_runner = get_dask_runner(
        cluster="example_slurm", extra_cluster_kwargs={"name": "jack-be-testing"}
    )
    assert isinstance(dask_task_runner, DaskTaskRunner)


def test_example_cluster_condif(tmpdir):
    """Load in a passable cluster configuration."""
    data = get_cluster_spec(cluster="example_slurm")
    assert data["cluster_class"] == "dask_jobqueue.SLURMCluster"


def test_example_cluster_no_exists():
    """Similar to the above but make sure an error is raised"""

    with pytest.raises(ValueError):
        get_cluster_spec(cluster="Jack-be-sneaky")
