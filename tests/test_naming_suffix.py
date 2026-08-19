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

    suffix = SuffixSpec(image=True)

    result = suffix + file_path
    assert isinstance(result, SuffixSpec)
    assert result.image
    assert result.linmos

    result = file_path + suffix
    assert isinstance(result, SuffixSpec)
    assert result.image
    assert result.linmos


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

    suffix = SuffixSpec(image=True)

    result = suffix | file_path
    assert isinstance(result, SuffixSpec)
    assert result.image
    assert result.linmos

    result = file_path | suffix
    assert isinstance(result, SuffixSpec)
    assert result.image
    assert result.linmos
