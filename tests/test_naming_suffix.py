"""Some tests around the suffix specification scheme
that is used to build names"""

from __future__ import annotations

from flint.naming import SuffixSpec


def test_add_suffix() -> None:
    """Combining two instances of suffix spec yield union"""


suffix_1 = SuffixSpec(image=True)
suffix_2 = SuffixSpec(linmos=True)
expected = SuffixSpec(image=True, linmos=True)
assert expected == (suffix_1 + suffix_2)
