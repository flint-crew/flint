"""Some tests around the suffix specification scheme
that is used to build names"""

from __future__ import annotations

import operator
from pathlib import Path

import pytest

from flint.naming import (
    ProcessedNameComponents,
    Suffix,
    create_path_from_processed_name_components,
    processed_ms_format,
)


def test_add_suffix() -> None:
    """Combining two instances using the add operator of suffix spec yield union"""

    suffix_1 = Suffix(image=True)
    suffix_2 = Suffix(linmos=True)
    expected = Suffix(image=True, linmos=True)
    result = suffix_1 + suffix_2
    assert isinstance(result, Suffix)
    assert result.image
    assert result.linmos
    assert expected == result


def test_or_suffix() -> None:
    """Combining two instances using the or keyword of suffix spec yield union"""

    suffix_1 = Suffix(image=True)
    suffix_2 = Suffix(linmos=True)
    expected = Suffix(image=True, linmos=True)
    result = suffix_1 | suffix_2
    assert isinstance(result, Suffix)
    assert result.image
    assert result.linmos
    assert expected == result


def test_subtact_suffix() -> None:
    """Combining two instances using the or keyword of suffix spec yield union"""

    suffix_1 = Suffix(image=True)
    suffix_2 = Suffix(image=True)
    expected = Suffix(image=False)
    result = suffix_1 - suffix_2
    assert isinstance(result, Suffix)
    assert not result.image
    assert expected == result

    suffix_1 = Suffix(image=True, linmos=True)
    suffix_2 = Suffix(image=True)
    expected = Suffix(image=False, linmos=True)
    result = suffix_1 - suffix_2
    assert isinstance(result, Suffix)
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

    suffix = Suffix(image=True)

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

    suffix = Suffix(image=True)

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

    suffix = Suffix(image=True)

    result = file_path - suffix
    assert isinstance(result, Path)
    assert result == updated_path


def test_and_suffix_with_path() -> None:
    """Create a Path object, and try oring a suffix to it using
    the ``and`` joining mode"""

    pcn = ProcessedNameComponents(
        sbid="123",
        field="Jack-Sparrow",
        round="1",
        image=True,
        linmos=True,
    )
    file_path = create_path_from_processed_name_components(
        processed_name_components=pcn
    )
    expected_path = Path("SB123.Jack-Sparrow.round1.image.linmos")
    assert file_path == expected_path
    updated_path = Path("SB123.Jack-Sparrow.round1.image")

    suffix = Suffix(image=True)

    result = file_path & suffix
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


@pytest.mark.parametrize("field", list(Suffix.model_fields))
def test_every_suffix_field_round_trips(field: str) -> None:
    """Each field declared on Suffix has to survive a generate and parse cycle.
    Fields were previously matched by the regex but never carried onto the
    components container."""
    name = create_path_from_processed_name_components(
        processed_name_components=ProcessedNameComponents(
            sbid="1234", field="Jack-Sparrow"
        ),
        suffix_spec=Suffix(**{field: True}),
    )
    assert name == Path(f"SB1234.Jack-Sparrow.{field}")

    pcn = processed_ms_format(Path(f"{name}.fits"))
    assert pcn is not None
    assert getattr(pcn, field) is True
    assert pcn.suffix_spec == Suffix(**{field: True})


@pytest.mark.parametrize(
    "operation", [operator.add, operator.or_, operator.sub, operator.and_]
)
def test_suffix_on_left_of_path_rejected(operation) -> None:
    """A Path is always the left operand, otherwise the operand order of a
    remove reads backwards"""
    file_path = Path("SB123.Jack-Sparrow.round1.linmos")

    with pytest.raises(TypeError):
        operation(Suffix(image=True), file_path)
