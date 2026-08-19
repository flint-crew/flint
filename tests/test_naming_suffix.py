"""Some tests around the suffix specification scheme
that is used to build names"""

from __future__ import annotations

from flint.naming import SuffixSpec


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
