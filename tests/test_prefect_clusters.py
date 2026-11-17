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


def test_performance_report_saved(tmp_path):
    """A flow run on a runner with a performance_report_dir saves its report"""
    from prefect import flow, task
    from prefect.client.orchestration import get_client

    from flint.prefect.clusters import FlintDaskTaskRunner

    @task
    def add_one(x: int) -> int:
        return x + 1

    @flow
    def a_flow() -> int:
        return add_one.submit(1).result()

    runner = FlintDaskTaskRunner(
        cluster_kwargs=dict(n_workers=1, processes=False),
        performance_report_dir=tmp_path / "reports",
    )
    state = a_flow.with_options(task_runner=runner)(return_state=True)
    assert state.result() == 2

    with get_client(sync_client=True) as client:
        flow_run = client.read_flow_run(state.state_details.flow_run_id)

    report = tmp_path / "reports" / f"dask-performance-report-{flow_run.name}.html"
    assert report.exists()


def test_no_performance_report_by_default():
    """Without a performance_report_dir no report is made"""
    dask_task_runner = get_dask_runner(cluster="example_slurm")
    assert dask_task_runner.performance_report_dir is None
    assert dask_task_runner.duplicate().performance_report_path is None
