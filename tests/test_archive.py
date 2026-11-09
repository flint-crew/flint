"""Tests around archives"""

from __future__ import annotations

import hashlib
import os
import tarfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from flint import archive
from flint.archive import (
    archive_options_from_strategy,
    archive_to,
    check_archive_prerequisites,
    cli,
    copy_files_into,
    create_sbid_tar_archive,
    get_archive_options_from_yaml,
    get_parser,
    matching_files,
    rclone_copy,
    resolve_glob_expressions,
    tar_files_into,
    write_tarball,
)
from flint.options import (
    DEFAULT_COPY_RE_PATTERNS,
    DEFAULT_TAR_RE_PATTERNS,
    ArchiveDestination,
    ArchiveOptions,
)
from flint.prefect.common.utils import task_archive_sbid
from flint.utils import get_packaged_resource_path

FILES = [f"some_file_{a:02d}-MFS-image.fits" for a in range(36)] + [
    f"a_validation.{ext}" for ext in ("png", "jpeg", "pdf")
]


@pytest.fixture
def glob_files(tmpdir):
    """Create an example set of temporary files in a known directory"""
    for f in FILES:
        touch_file = f"{tmpdir / f!s}"
        with open(touch_file, "w") as out_file:
            out_file.write("example\n")

    return (tmpdir, FILES)


@pytest.fixture
def temp_files(glob_files):
    base_dir, files = glob_files

    archive_options = ArchiveOptions()

    resolved = resolve_glob_expressions(
        base_path=base_dir, file_re_patterns=archive_options.tar_file_re_patterns
    )

    return (base_dir, resolved)


@pytest.fixture
def package_strategy_path():
    example = get_packaged_resource_path(
        package="flint", filename="data/tests/test_config.yaml"
    )

    return example


def test_get_archive_options_from_yaml(package_strategy_path):
    """Get the options from a strategy yaml file for ArchiveOptions"""
    options = get_archive_options_from_yaml(
        strategy_yaml_path=Path(package_strategy_path)
    )

    assert isinstance(options, dict)
    assert options["tar_file_re_patterns"][-1] == "testing_for_jack.txt"
    assert options["copy_file_re_patterns"][-1] == "testing_for_sparrow.csv"
    assert len(options["tar_file_re_patterns"]) == 6


def test_copy_files_into(tmpdir, temp_files):
    """Basic sanity checks to the copying process"""
    base_dir, files = temp_files

    tmpdir = Path(tmpdir)
    copy_out = tmpdir / "copy_location"

    copy_files_into(copy_out_path=copy_out, files_to_copy=files)

    files_copied = list(copy_out.glob("*"))
    assert len(files_copied) == len(files)
    assert len(files_copied) > 0


def test_glob_expressions(glob_files):
    """Trying out the globbing from the archive options"""
    base_dir, files = glob_files

    archive_options = ArchiveOptions()
    assert len(archive_options.tar_file_re_patterns) > 0

    resolved = resolve_glob_expressions(
        base_path=base_dir, file_re_patterns=archive_options.tar_file_re_patterns
    )

    assert all([isinstance(p, Path) for p in resolved])
    assert len(resolved) == 37


def test_glob_expressions_uniq(glob_files):
    """Make sure that the uniqueness is correct"""
    base_dir, files = glob_files

    archive_options = ArchiveOptions(tar_file_re_patterns=(".*png", ".*png"))
    resolved = resolve_glob_expressions(
        base_path=base_dir, file_re_patterns=archive_options.tar_file_re_patterns
    )
    assert len(resolved) == 1


def test_glob_expressions_empty(glob_files):
    """Make sure that the uniqueness is correct"""
    base_dir, files = glob_files

    archive_options = ArchiveOptions(tar_file_re_patterns=(".*doesnotexist",))
    resolved = resolve_glob_expressions(
        base_path=base_dir, file_re_patterns=archive_options.tar_file_re_patterns
    )
    assert len(resolved) == 0


def test_archive_parser(glob_files):
    """Make sure pirates understand parsers"""
    base_dir, files = glob_files
    parser = get_parser()

    args = parser.parse_args("list".split())

    assert isinstance(args.base_path, Path)
    assert args.tar_file_re_patterns == DEFAULT_TAR_RE_PATTERNS

    example_path = Path("this/no/exist")
    args = parser.parse_args(f"list --base-path {example_path!s}".split())
    assert isinstance(args.base_path, Path)
    assert args.base_path == example_path

    args = parser.parse_args(
        r"list --copy-file-re-patterns '.*linmos.*' '.*MFS.*'".split()
    )
    assert len(args.copy_file_re_patterns) == 2

    example_path = Path(base_dir)
    args = parser.parse_args(
        f"list --base-path {example_path!s} --copy-file-re-patterns *pdf".split()
    )
    assert isinstance(args.base_path, Path)
    assert args.base_path == example_path
    assert args.copy_file_re_patterns == ["*pdf"]

    cmd = r"create --tar-file-re-patterns '.*linmos.*' '.*MFS.*' '.*beam[0-9]+\.round4-????-image\.fits' --base-path 39420 test_archive_tarball/39420.tar"
    args = parser.parse_args(cmd.split())
    assert len(args.tar_file_re_patterns) == 3


def test_tar_ball_files(temp_files):
    """Ensure that the tarballing works"""
    base_dir, files = temp_files

    tar_out_path = Path(base_dir) / "some_tarball.tar"
    _ = tar_files_into(tar_out_path=tar_out_path, files_to_tar=files)

    assert tarfile.is_tarfile(tar_out_path)

    with pytest.raises(FileExistsError):
        _ = tar_files_into(tar_out_path=tar_out_path, files_to_tar=files)


def test_create_sbid_archive(glob_files):
    """Attempts to ensure the entire find files and tarball creation works"""
    base_dir, files = glob_files

    archive_options = ArchiveOptions()
    tar_out_path = Path(base_dir) / "example_tarball_2.tar"

    _ = create_sbid_tar_archive(
        base_path=base_dir, tar_out_path=tar_out_path, archive_options=archive_options
    )

    assert tarfile.is_tarfile(tar_out_path)


def test_cli_create_without_a_strategy(glob_files):
    """The patterns come from the command line when no strategy file is given"""
    base_dir, files = glob_files
    tar_out_path = Path(base_dir) / "from_the_cli.tar"

    cli(
        [
            "create",
            str(tar_out_path),
            "--base-path",
            str(base_dir),
            "--tar-file-re-patterns",
            ".*png",
        ]
    )

    with tarfile.open(tar_out_path) as tar:
        assert tar.getnames() == ["a_validation.png"]


def test_archive_sbid_round0(tmp_path):
    """The products of a round 0, such as a sky-model calibration, are archived when it is the last round"""
    science_path = tmp_path / "12345"
    science_path.mkdir()
    final_ms = science_path / "SB12345.RACS_0000+00.beam00.round0.ms.zip"
    final_ms.touch()
    archive_path = tmp_path / "archive"
    archive_path.mkdir()

    task_archive_sbid.fn(
        science_folder_path=science_path, archive_path=archive_path, max_round=0
    )

    (tar_path,) = archive_path.glob("*.tar")
    with tarfile.open(tar_path) as tar:
        assert final_ms.name in {Path(name).name for name in tar.getnames()}


def test_archive_new_tar_patterns():
    """Some sanity checks around this archive options tuple updating"""
    archive_options = ArchiveOptions()
    before_count = len(archive_options.tar_file_re_patterns)

    additional_file_patterns = (r".*beam[0-9]+\.round4-[0-9]{4}-image\.fits",)
    new_patterns = archive_options.tar_file_re_patterns + additional_file_patterns

    new_archive_options = archive_options.with_options(
        tar_file_re_patterns=new_patterns
    )
    assert len(new_archive_options.tar_file_re_patterns) == before_count + 1


def test_archiveoptions_with_options():
    """Ensure that the with_options interface for ArchiveOptions works"""

    archive_options = ArchiveOptions()
    default_copy = archive_options.copy_file_re_patterns
    update_options = ("Jack", "was", "here")
    new_options = archive_options.with_options(copy_file_re_patterns=update_options)

    assert new_options.copy_file_re_patterns != default_copy
    assert new_options.copy_file_re_patterns == update_options
    assert archive_options is not new_options

    new_dict = new_options._asdict()
    assert new_dict["copy_file_re_patterns"] == update_options


def test_copy_patterns_match_compressed_cubes(tmp_path):
    """Compressed linmos and weight cubes are archived like the plain ones"""
    names = [
        "SB1.field.i.linmos.fits",
        "SB1.field.i.linmos.fits.gz",
        "SB1.field.i.linmos.fits.fz",
        "SB1.field.i.weight.fits.fz",
    ]
    for name in names:
        (tmp_path / name).touch()

    resolved = resolve_glob_expressions(
        base_path=tmp_path, file_re_patterns=DEFAULT_COPY_RE_PATTERNS
    )

    assert sorted(path.name for path in resolved) == sorted(names)


@pytest.fixture
def field(tmp_path: Path) -> Path:
    """A small field folder: an image, a zarr store and an unwanted file"""
    root = tmp_path / "60001"
    for name in (
        "SB60001.i.linmos.fits",
        "SB60001.beam00.round2.ms.tar",
        "rmsynth/fdf.dirty.zarr/zarr.json",
        "rmsynth/fdf.dirty.zarr/c/0/0",
        "scratch.txt",
    ):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(f"contents of {name}")

    return root


def test_matching_files_matches_relative_paths_whole(field: Path) -> None:
    files = matching_files(root=field, patterns=[r".*\.linmos\.fits", r"rmsynth/.*"])

    assert files == [
        Path("SB60001.i.linmos.fits"),
        Path("rmsynth/fdf.dirty.zarr/c/0/0"),
        Path("rmsynth/fdf.dirty.zarr/zarr.json"),
    ]
    # Unanchored, as re.search would take it
    assert matching_files(root=field, patterns=[r"linmos"]) == []


def test_write_tarball_names_members_under_the_field(
    field: Path, tmp_path: Path
) -> None:
    files = matching_files(root=field, patterns=[r".*"])

    tar_path = write_tarball(
        root=field, files=files, tar_path=tmp_path / "tape/SB60001.tar"
    )

    with tarfile.open(tar_path) as tar:
        assert tar.getnames()[:2] == [
            "SB60001/SB60001.beam00.round2.ms.tar",
            "SB60001/SB60001.i.linmos.fits",
        ]
    md5, name = (tmp_path / "tape/SB60001.tar.md5").read_text().split()
    assert (md5, name) == (
        hashlib.md5(tar_path.read_bytes()).hexdigest(),
        "SB60001.tar",
    )
    assert not list((tmp_path / "tape").glob("*.partial"))


def test_write_tarball_keeps_an_existing_tarball(field: Path, tmp_path: Path) -> None:
    tar_path = tmp_path / "SB60001.tar"
    tar_path.write_text("finished earlier")

    write_tarball(root=field, files=[Path("scratch.txt")], tar_path=tar_path)

    assert tar_path.read_text() == "finished earlier"


def _rclone_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(
        archive, "run_singularity_command", lambda **kwargs: calls.append(kwargs)
    )
    return calls


def test_rclone_copy_to_a_local_folder(
    field: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _rclone_calls(monkeypatch)
    hot = tmp_path / "hot" / "SB60001"

    rclone_copy(
        container=tmp_path / "rclone.sif",
        config=None,
        root=field,
        files=[Path("SB60001.i.linmos.fits")],
        location=str(hot),
    )

    (call,) = calls
    command = call["command"].split()
    assert command[:2] == ["rclone", "copy"]
    assert command[-2:] == [str(field.resolve()), str(hot)]
    assert {"--checksum", "--immutable"} <= set(command)
    assert hot.is_dir()
    assert hot.resolve() in call["bind_dirs"]
    assert call["read_only_bind_dirs"] == [field.resolve()]


def test_rclone_copy_to_a_remote_binds_the_config_read_only(
    field: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _rclone_calls(monkeypatch)
    config = tmp_path / "rclone.conf"

    rclone_copy(
        container=tmp_path / "rclone.sif",
        config=config,
        root=field,
        files=[Path("SB60001.i.linmos.fits")],
        location="acacia:racs-all/SB60001",
    )

    (call,) = calls
    assert call["command"].startswith(f"rclone --config {config.resolve()} copy ")
    assert call["command"].endswith(" acacia:racs-all/SB60001")
    assert config.resolve() in call["read_only_bind_dirs"]
    assert len(call["bind_dirs"]) == 1  # the file listing only


def test_archive_to_each_method(
    field: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _rclone_calls(monkeypatch)
    tape = ArchiveDestination(
        method="tar", location=str(tmp_path / "tape"), patterns=(".*",)
    )
    acacia = ArchiveDestination(
        method="copy", location="acacia:racs-all", patterns=(r".*\.fits",)
    )

    archive_to(root=field, field="SB60001", destination=tape)
    archive_to(root=field, field="SB60001", destination=tape.with_options(suffix=".ms"))
    archive_to(
        root=field, field="SB60001", destination=acacia, rclone_container=tmp_path
    )

    assert (tmp_path / "tape" / "SB60001.tar").exists()
    assert (tmp_path / "tape" / "SB60001.ms.tar").exists()
    assert calls[0]["command"].endswith(" acacia:racs-all/SB60001")
    with pytest.raises(ValueError, match="rclone container"):
        archive_to(root=field, field="SB60001", destination=acacia)


def test_archive_to_refuses_a_missing_root_or_nothing_matching(
    field: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run must not pass with a product left behind"""
    calls = _rclone_calls(monkeypatch)
    nothing = ArchiveDestination(method="copy", location="a:b", patterns=(r".*\.nope",))

    with pytest.raises(FileNotFoundError, match="does not exist"):
        archive_to(root=tmp_path / "missing", field="SB1", destination=nothing)
    with pytest.raises(ValueError, match="No file under"):
        archive_to(root=field, field="SB60001", destination=nothing)

    assert calls == []


def test_destination_locations_and_targets() -> None:
    """A tar needs a folder; a copy takes a folder or a remote; the suffix keeps
    destinations sharing a location apart"""
    tape = ArchiveDestination(method="tar", location="/tape", patterns=(".*",))
    remote = ArchiveDestination(method="copy", location="acacia:a", patterns=(".*",))

    assert tape.target("SB1") == "/tape/SB1.tar"
    assert tape.with_options(suffix=".ms").target("SB1") == "/tape/SB1.ms.tar"
    assert remote.target("SB1") == "acacia:a/SB1"
    assert remote.remote and not tape.remote
    with pytest.raises(ValidationError, match="absolute folder path"):
        ArchiveDestination(method="tar", location="acacia:a", patterns=(".*",))
    with pytest.raises(ValidationError, match="rclone remote"):
        ArchiveDestination(method="copy", location="relative/hot", patterns=(".*",))


def test_check_archive_prerequisites(tmp_path: Path) -> None:
    container, config = tmp_path / "rclone.sif", tmp_path / "rclone.conf"
    hot = ArchiveDestination(
        method="copy", location=str(tmp_path / "hot"), patterns=(".*",)
    )
    remote = ArchiveDestination(method="copy", location="acacia:a", patterns=(".*",))
    tape = ArchiveDestination(
        method="tar", location=str(tmp_path / "tape"), patterns=(".*",)
    )

    check_archive_prerequisites([tape], rclone_container=None, rclone_config=None)
    with pytest.raises(ValueError, match="rclone container"):
        check_archive_prerequisites(
            [hot], rclone_container=container, rclone_config=None
        )
    container.touch()
    check_archive_prerequisites([hot], rclone_container=container, rclone_config=None)
    with pytest.raises(ValueError, match="rclone config"):
        check_archive_prerequisites(
            [remote], rclone_container=container, rclone_config=config
        )
    config.touch()
    check_archive_prerequisites(
        [remote], rclone_container=container, rclone_config=config
    )


def test_prerequisites_refuse_destinations_sharing_a_target(tmp_path: Path) -> None:
    tape = ArchiveDestination(
        method="tar", location=str(tmp_path), patterns=(r".*\.fits",)
    )
    tape_ms = tape.with_options(patterns=(r".*\.ms\.tar",))

    with pytest.raises(ValueError, match="2 destinations would all write"):
        check_archive_prerequisites(
            [tape, tape_ms], rclone_container=None, rclone_config=None
        )
    check_archive_prerequisites(
        [tape, tape_ms.with_options(suffix=".ms")],
        rclone_container=None,
        rclone_config=None,
    )


def test_prerequisites_refuse_a_location_that_cannot_be_written(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    if os.access(locked, os.W_OK):
        pytest.skip("running as a user who can write anywhere")
    tape = ArchiveDestination(
        method="tar", location=str(locked / "deeper" / "tape"), patterns=(".*",)
    )

    with pytest.raises(ValueError, match="cannot be written to"):
        check_archive_prerequisites([tape], rclone_container=None, rclone_config=None)


def test_archive_options_from_a_strategy_without_selfcal(tmp_path: Path) -> None:
    """flint_flow_archive takes a strategy that only says where things go"""
    strategy = tmp_path / "archive.yaml"
    strategy.write_text(
        "version: 0.2\n"
        "defaults:\n"
        "  archive:\n"
        "    destinations:\n"
        f"      tape: {{method: tar, location: {tmp_path}, patterns: ['.*']}}\n"
    )

    options = archive_options_from_strategy(strategy_yaml_path=strategy)

    assert options.destinations["tape"].method == "tar"
    (tmp_path / "bare.yaml").write_text("version: 0.2\ndefaults: {}\n")
    assert archive_options_from_strategy(tmp_path / "bare.yaml").destinations == {}


@pytest.fixture(scope="session")
def rclone_image(tmp_path_factory) -> Path:
    """Pull the rclone container once for the session"""
    from flint.containers import rclone_container
    from flint.sclient import pull_container

    return pull_container(
        container_directory=Path(tmp_path_factory.mktemp("rclone")),
        uri=f"{rclone_container.uri}:{rclone_container.tag}",
        file_name=rclone_container.file_name,
    )


@pytest.mark.require_singularity
def test_rclone_copies_to_a_folder_and_a_remote(
    field: Path, tmp_path: Path, rclone_image: Path
) -> None:
    """A ``type = local`` remote stands in for an object store. Its folder is under
    /tmp, which the container sees without a bind of its own."""
    from subprocess import CalledProcessError

    files = matching_files(root=field, patterns=[r".*\.fits", r"rmsynth/.*"])
    config = tmp_path / "rclone.conf"
    config.write_text("[store]\ntype = local\n")
    hot = tmp_path / "hot" / "SB60001"
    remote = f"store:{tmp_path / 'store'}/SB60001"

    rclone_copy(
        container=rclone_image, config=None, root=field, files=files, location=str(hot)
    )
    rclone_copy(
        container=rclone_image, config=config, root=field, files=files, location=remote
    )

    for copied in (hot, tmp_path / "store" / "SB60001"):
        assert (
            copied / "SB60001.i.linmos.fits"
        ).read_text() == "contents of SB60001.i.linmos.fits"
        assert (copied / "rmsynth" / "fdf.dirty.zarr" / "c" / "0" / "0").exists()
        assert not (copied / "scratch.txt").exists()

    # A rerun compares and copies nothing; a source that changed is refused
    rclone_copy(
        container=rclone_image, config=None, root=field, files=files, location=str(hot)
    )
    (field / "SB60001.i.linmos.fits").write_text("reprocessed")
    with pytest.raises(CalledProcessError):
        rclone_copy(
            container=rclone_image,
            config=None,
            root=field,
            files=files,
            location=str(hot),
        )
