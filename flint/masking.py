"""Flint-specific utilities around masking. Masks themselves are made by eye-patch."""

from __future__ import annotations

from collections.abc import Collection

from flint.logging import logger


def consider_beam_mask_round(
    current_round: int,
    mask_rounds: str | Collection[int] | int,
    allow_beam_masks: bool = True,
) -> bool:
    """Evaluate whether a self-calibration round should have a beam clean mask
    constructed. Rules are:

    - if `mask_rounds` is a string and is "all", all rounds will have a beam mask
    - if 'mask_rounds' is a single integer, so long as `current_round` is larger it will have a beam mask
    - if `mask_rounds` is iterable and contains `current_round` it will have a beam mask
    - if `allow_beam_masks` is False a False is returned. Otherwise options above are considered.

    Args:
        current_round (int): The current self-calibration round that is being performed
        mask_rounds (Union[str, Collection[int], int]): The rules to consider whether a beam mask is needed
        allow_beam_masks (bool, optional): A global allow / deny. This should be `True` for other rules to be considered. Defaults to True.

    Returns:
        bool: Whether per beam mask should be performed
    """
    logger.info(f"Considering {current_round=} {mask_rounds=} {allow_beam_masks=}")

    # The return below was getting silly to meantally parse
    if not allow_beam_masks:
        return False

    return mask_rounds is not None and (
        (isinstance(mask_rounds, str) and mask_rounds.lower() == "all")
        or (isinstance(mask_rounds, int) and current_round >= mask_rounds)
        or ((isinstance(mask_rounds, (list, tuple))) and current_round in mask_rounds)
    )  # type: ignore
