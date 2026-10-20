from __future__ import annotations

from flint.masking import consider_beam_mask_round


def test_consider_beam_masking_round():
    """Test to ensure the beam mask consideration log is correct"""
    lower = ("all", "ALL", "aLl")
    states = (
        consider_beam_mask_round(current_round=1, mask_rounds=low) for low in lower
    )

    assert all(states)

    assert consider_beam_mask_round(current_round=3, mask_rounds=1)
    assert not consider_beam_mask_round(current_round=0, mask_rounds=1)

    assert consider_beam_mask_round(current_round=3, mask_rounds=(1, 2, 3, 4, 5))
    assert not consider_beam_mask_round(current_round=3, mask_rounds=(1, 2, 4, 5))

    assert not consider_beam_mask_round(current_round=3, mask_rounds=None)

    assert not consider_beam_mask_round(
        current_round=3, mask_rounds=1, allow_beam_masks=False
    )
    assert consider_beam_mask_round(
        current_round=3, mask_rounds=1, allow_beam_masks=True
    )
    assert consider_beam_mask_round(current_round=1, mask_rounds=1)
