"""Some specialised tests around renaming wsclean output file names
into something consistent with flint naming"""

from __future__ import annotations

from flint.imager.wsclean import _rename_wsclean_title


def test_rename_wsclean_title() -> None:
    """An example of updating the name through processed component names"""

    ex = "SB56289.RACS_1041+18.beam15.round1.qu-0000-Q-image.fits"
    out_ex = "SB56289.RACS_1041+18.beam15.round1.q.ch0000-0001.image.fits"
    assert _rename_wsclean_title(name_str=ex) == out_ex

    ex = "SB56289.RACS_1041+18.beam15.round1.qu-0000-U-image.fits"
    out_ex = "SB56289.RACS_1041+18.beam15.round1.u.ch0000-0001.image.fits"
    assert _rename_wsclean_title(name_str=ex) == out_ex
