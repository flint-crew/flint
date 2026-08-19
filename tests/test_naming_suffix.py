"""Some tests around the suffix specification scheme
that is used to build names"""

from __future__ import annotations

from pathlib import Path

from flint.naming import (
    ProcessedNameComponents,
    SuffixSpec,
    create_path_from_processed_name_components,
)


def test_add_suffix() -> None:
    """Combining two instances using the add operator of suffix spec yield union"""

    suffix_1 = SuffixSpec(image=True)
    suffix_2 = SuffixSpec(linmos=True)
    expected = SuffixSpec(image=True, linmos=True)
    result = suffix_1 + suffix_2
    assert isinstance(result, SuffixSpec)
    assert result.image
    assert result.linmos
    assert expected == result


def test_or_suffix() -> None:
    """Combining two instances using the or keyword of suffix spec yield union"""

    suffix_1 = SuffixSpec(image=True)
    suffix_2 = SuffixSpec(linmos=True)
    expected = SuffixSpec(image=True, linmos=True)
    result = suffix_1 | suffix_2
    assert isinstance(result, SuffixSpec)
    assert result.image
    assert result.linmos
    assert expected == result


def test_subtact_suffix() -> None:
    """Combining two instances using the or keyword of suffix spec yield union"""

    suffix_1 = SuffixSpec(image=True)
    suffix_2 = SuffixSpec(image=True)
    expected = SuffixSpec(image=False)
    result = suffix_1 - suffix_2
    assert isinstance(result, SuffixSpec)
    assert not result.image
    assert expected == result

    suffix_1 = SuffixSpec(image=True, linmos=True)
    suffix_2 = SuffixSpec(image=True)
    expected = SuffixSpec(image=False, linmos=True)
    result = suffix_1 - suffix_2
    assert isinstance(result, SuffixSpec)
    assert not result.image
    assert result.linmos
    assert expected == result


def test_add_suffix_with_path() -> None:
    """Create a Path object, and try adding a suffix to it"""

    pcn = ProcessedNameComponents(
        sbid="123",
        field="Jack-Sparrow",
        round="1",
        linmos=True,
    )
    file_path = create_path_from_processed_name_components(
        processed_name_components=pcn
    )
    expected_path = Path("SB123.Jack-Sparrow.round1.linmos")
    assert file_path == expected_path

    updated_path = Path("SB123.Jack-Sparrow.round1.image.linmos")

    suffix = SuffixSpec(image=True)

    result = suffix + file_path
    assert isinstance(result, Path)
    assert result == updated_path

    result = file_path + suffix
    assert isinstance(result, Path)
    assert result == updated_path


def test_or_suffix_with_path() -> None:
    """Create a Path object, and try oring a suffix to it"""

    pcn = ProcessedNameComponents(
        sbid="123",
        field="Jack-Sparrow",
        round="1",
        linmos=True,
    )
    file_path = create_path_from_processed_name_components(
        processed_name_components=pcn
    )
    expected_path = Path("SB123.Jack-Sparrow.round1.linmos")
    assert file_path == expected_path
    updated_path = Path("SB123.Jack-Sparrow.round1.image.linmos")

    suffix = SuffixSpec(image=True)

    result = suffix | file_path
    assert isinstance(result, Path)
    assert result == updated_path

    result = file_path | suffix
    assert isinstance(result, Path)
    assert result == updated_path


def test_remove_suffix_with_path() -> None:
    """Create a Path object, and try oring a suffix to it"""

    pcn = ProcessedNameComponents(
        sbid="123", field="Jack-Sparrow", round="1", linmos=True, image=True
    )
    file_path = create_path_from_processed_name_components(
        processed_name_components=pcn
    )
    expected_path = Path("SB123.Jack-Sparrow.round1.image.linmos")
    assert file_path == expected_path
    updated_path = Path("SB123.Jack-Sparrow.round1.linmos")

    suffix = SuffixSpec(image=True)

    result = suffix - file_path
    assert isinstance(result, Path)
    assert result == updated_path

    result = file_path - suffix
    assert isinstance(result, Path)
    assert result == updated_path


def test_bad_pol_field() -> None:
    """When constructing the above tests a incorrect
    handling of a pol field was found. This is tracking
    that error down."""
    expected_path = Path("SB123.Jack-Sparrow.round1.image.linmos")
    from flint.naming import processed_ms_format

    # There was previously a bug that would allow something like the
    # `.image` ield to incorrectly be cast to a `pol`. This tests
    # was used to help diagnose.
    pcn = processed_ms_format(expected_path)
    assert pcn is not None
    assert pcn.pol is None
