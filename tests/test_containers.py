"""Items to test functionality around the containers interface"""

from __future__ import annotations

from pathlib import Path

import pytest

import flint.containers
from flint.containers import (
    KNOWN_CONTAINER_LOOKUP,
    LIST_OF_KNOWN_CONTAINERS,
    FlintContainer,
    _sanity_check_containers,
    cpu_level_from_flags,
    detect_cpu_level,
    download_known_containers,
    get_container_tag,
    get_known_container_path,
    get_parser,
    log_known_containers,
    parse_cpuinfo_flags,
    resolve_cpu_level,
    verify_known_containers,
)

V2_FLAGS = "fpu sse2 cx16 lahf_lm popcnt sse4_1 sse4_2 ssse3"
V3_FLAGS = f"{V2_FLAGS} avx avx2 bmi1 bmi2 f16c fma abm movbe xsave"
V4_FLAGS = f"{V3_FLAGS} avx512f avx512bw avx512cd avx512dq avx512vl"


def _cpuinfo(flags: str) -> str:
    """Two processors' worth of x86 /proc/cpuinfo text with the given flags"""
    return "".join(
        f"processor\t: {idx}\nmodel name\t: Test CPU\nflags\t\t: {flags}\nbugs\t\t: spectre_v1\n\n"
        for idx in range(2)
    )


def test_sanity_check_containers():
    """Make sure that the reference containers are valid with a simple set of checks.
    Make sure not doubling up on the reference name of file name"""
    _sanity_check_containers(container_list=LIST_OF_KNOWN_CONTAINERS)
    from copy import deepcopy

    dummy = list(deepcopy(LIST_OF_KNOWN_CONTAINERS))
    dummy.append(dummy[0])

    with pytest.raises(AssertionError):
        _sanity_check_containers(container_list=dummy)


def test_verify_known_containers(tmpdir):
    """Check that our verify function works. This will cheat and
    create some temporary file with the expected file name as the
     verify function in of itself (currently) only checks to see
     if a file exists"""
    container_directory = Path(tmpdir) / "containers1"
    container_directory.mkdir(parents=True)

    assert not verify_known_containers(container_directory=container_directory)

    for cata in LIST_OF_KNOWN_CONTAINERS:
        cata_path = container_directory / cata.file_name
        cata_path.touch()

    assert verify_known_containers(container_directory=container_directory)


def test_all_flint_containers():
    """Make sure everything we know is a FlintContainer"""
    assert all([isinstance(fc, FlintContainer) for fc in LIST_OF_KNOWN_CONTAINERS])


def test_all_known_containers():
    """Same as above but for the known donctainers lookup"""
    for k, v in KNOWN_CONTAINER_LOOKUP.items():
        assert isinstance(k, str)
        assert isinstance(v, FlintContainer)


def test_log_containers():
    """Output all the known containers"""
    # This should simply not error
    log_known_containers()


@pytest.mark.slow
@pytest.mark.require_singularity
def test_download_flint_containers(flint_containers) -> None:
    """Start the download of the flint containers"""
    assert isinstance(flint_containers, Path)
    assert flint_containers.exists()


@pytest.mark.slow
@pytest.mark.require_singularity
def test_verify_containers_with_containers(flint_containers):
    """Make sure the actual containers downloaded register as correct. This
    uses the download fixture"""
    assert verify_known_containers(container_directory=flint_containers)


@pytest.mark.slow
@pytest.mark.require_singularity
def test_get_known_container_path(flint_containers):
    """Get the to a known container"""
    casa_container = get_known_container_path(
        container_directory=flint_containers, name="casa"
    )
    assert isinstance(casa_container, Path)
    assert casa_container.exists()

    with pytest.raises(ValueError):
        casa_container = get_known_container_path(
            container_directory=flint_containers, name="JackSparrowNotBeKnown"
        )


def test_parse_cpuinfo_flags():
    """Flags come from the x86 ``flags`` line, and ARM text has none"""
    assert parse_cpuinfo_flags(_cpuinfo(V3_FLAGS)) == set(V3_FLAGS.split())
    assert parse_cpuinfo_flags("processor\t: 0\nFeatures\t: fp asimd\n") == set()
    assert parse_cpuinfo_flags("") == set()


@pytest.mark.parametrize(
    "flags, level",
    [
        ("", "portable"),
        (V2_FLAGS, "portable"),
        (V3_FLAGS.replace(" avx2", ""), "portable"),
        (V3_FLAGS, "x86-64-v3"),
        (f"{V3_FLAGS} avx512f", "x86-64-v3"),
        (V4_FLAGS, "x86-64-v4"),
        (V4_FLAGS.replace(" avx2", ""), "portable"),
    ],
)
def test_cpu_level_from_flags(flags, level):
    """Each level needs all of its flags and those of the levels below"""
    assert cpu_level_from_flags(flags.split()) == level


@pytest.mark.parametrize(
    "flags, machine, level",
    [
        (V3_FLAGS, "x86_64", "x86-64-v3"),
        (V4_FLAGS, "x86_64", "x86-64-v4"),
        (V4_FLAGS, "AMD64", "x86-64-v4"),
        (V4_FLAGS, "aarch64", "portable"),
        (V4_FLAGS, "arm64", "portable"),
    ],
)
def test_detect_cpu_level(tmp_path, flags, machine, level):
    """Read the level from a cpuinfo file, falling back to portable off x86"""
    cpuinfo_path = tmp_path / "cpuinfo"
    cpuinfo_path.write_text(_cpuinfo(flags))
    assert detect_cpu_level(cpuinfo_path=cpuinfo_path, machine=machine) == level


def test_detect_cpu_level_missing_cpuinfo(tmp_path):
    """No cpuinfo means portable"""
    assert (
        detect_cpu_level(cpuinfo_path=tmp_path / "missing", machine="x86_64")
        == "portable"
    )


def test_detect_cpu_level_this_machine():
    """Whatever this machine is, it gets a known level"""
    assert detect_cpu_level() in flint.containers.CPU_LEVELS


def test_resolve_cpu_level(monkeypatch):
    """auto uses the detected level, known levels pass through, others are rejected"""
    monkeypatch.setattr(flint.containers, "detect_cpu_level", lambda: "x86-64-v4")
    assert resolve_cpu_level("auto") == "x86-64-v4"
    assert resolve_cpu_level("x86-64-v3") == "x86-64-v3"
    assert resolve_cpu_level("portable") == "portable"
    with pytest.raises(ValueError):
        resolve_cpu_level("x86-64-v2")


@pytest.mark.parametrize(
    "cpu_level, tag",
    [
        ("portable", "v1.2.3"),
        ("x86-64-v3", "v1.2.3-x86-64-v3"),
        ("x86-64-v4", "v1.2.3-x86-64-v4"),
    ],
)
def test_get_container_tag(cpu_level, tag):
    """Non-portable levels are a tag suffix"""
    assert get_container_tag(tag="v1.2.3", cpu_level=cpu_level) == tag


def test_parser_cpu():
    """The download command takes --cpu, defaulting to portable"""
    parser = get_parser()
    args = parser.parse_args(["download", "containers"])
    assert args.cpu == "portable"
    assert args.tag is None

    for cpu in ("portable", "x86-64-v3", "x86-64-v4", "auto"):
        args = parser.parse_args(["download", "containers", "--cpu", cpu])
        assert args.cpu == cpu

    with pytest.raises(SystemExit):
        parser.parse_args(["download", "containers", "--cpu", "x86-64-v2"])


@pytest.fixture
def pulled_uris(monkeypatch):
    """Record the URIs that would be pulled instead of pulling them"""
    uris = []

    def _fake_pull(container_directory, uri, file_name):
        uris.append(uri)
        path = Path(container_directory) / file_name
        path.touch()
        return path

    monkeypatch.setattr(flint.containers, "pull_container", _fake_pull)
    return uris


@pytest.mark.parametrize(
    "kwargs, suffix",
    [
        ({}, ":v1.0"),
        ({"new_tag": "v2.0.0"}, ":v2.0.0"),
        ({"new_tag": "v2.0.0", "cpu": "portable"}, ":v2.0.0"),
        ({"new_tag": "v2.0.0", "cpu": "x86-64-v3"}, ":v2.0.0-x86-64-v3"),
        ({"new_tag": "v2.0.0", "cpu": "x86-64-v4"}, ":v2.0.0-x86-64-v4"),
        ({"cpu": "x86-64-v4"}, ":v1.0-x86-64-v4"),
    ],
)
def test_download_tag_selection(tmp_path, pulled_uris, kwargs, suffix):
    """Every known container is pulled with the tag for the chosen CPU level"""
    paths = download_known_containers(container_directory=tmp_path, **kwargs)

    assert len(paths) == len(LIST_OF_KNOWN_CONTAINERS)
    assert sorted(pulled_uris) == sorted(
        f"{container.uri}{suffix}" for container in LIST_OF_KNOWN_CONTAINERS
    )


def test_download_tag_selection_auto(tmp_path, pulled_uris, monkeypatch):
    """auto pulls the detected level"""
    monkeypatch.setattr(flint.containers, "detect_cpu_level", lambda: "x86-64-v3")
    download_known_containers(
        container_directory=tmp_path, new_tag="v2.0.0", cpu="auto"
    )

    assert pulled_uris
    assert all(uri.endswith(":v2.0.0-x86-64-v3") for uri in pulled_uris)


def test_download_bad_cpu(tmp_path, pulled_uris):
    """An unknown CPU level fails before anything is pulled"""
    with pytest.raises(ValueError):
        download_known_containers(container_directory=tmp_path, cpu="avx2")
    assert pulled_uris == []
