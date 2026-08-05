"""Solve for wsclean output channel divisions that make a single compact cube.

wsclean declares ``CRVAL3 = (f[a] + f[b]) / 2`` for an output image spanning
input channel centres ``f[a] .. f[b]`` (the ``bandStart/bandEnd`` half-widths in
``WSClean::makeImagingTableEntryChannelSettings`` cancel in
``io/wscfitswriter.cpp``). The declared frequency therefore tracks the channels
that are actually in the bin, not the requested boundary, so a bin straddling a
sub-band gap is centred out in the gap where there is no data.

``fitscube`` can only write a linear FREQ axis: ``even_spacing`` takes the
tolerant gcd of the input centre diffs as ``CDELT3``, so one gap-straddling
image collapses the step and pads the cube with thousands of blank planes.

This module partitions the channel list (union of centres across all MSs, which
is what wsclean itself indexes) such that every predicted centre lands on one
uniform grid, no channel is dropped, and padding is minimised.

Uniformity condition: with ``q[i]`` the index of channel ``i`` on the native
grid of width ``W``, a bin ``[a, b]`` sits on a grid of step ``t * W / 2`` iff
``(q[a] + q[b]) % t`` is the same for every bin. Every ``(t, phase)`` that admits
a partition gives a candidate; the least padded, most uniform one is returned.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import numpy as np
from astropy import units as u
from capn_crunch import BaseOptions
from fitscube.combine_fits import even_spacing

from flint.exceptions import ChannelDivisionError
from flint.logging import logger

if TYPE_CHECKING:
    from flint.ms import MS

MAX_PADDING_FACTOR = 1.25
"""How far above the span / bandwidth padding floor a cube is allowed to be"""


class ChannelDivision(BaseOptions):
    """A wsclean output channel division and the cube it will produce"""

    channels_out: int
    """Value for the wsclean ``-channels-out`` argument"""
    channel_division_frequencies: tuple[float, ...]
    """Value for the wsclean ``-channel-division-frequencies`` argument, in Hz. One per split, i.e. ``channels_out - 1`` values"""
    predicted_frequencies: tuple[float, ...]
    """The ``CRVAL3`` of each output image, in Hz, in wsclean order"""
    bin_channels: tuple[int, ...]
    """Number of input channels in each output image"""
    cdelt3: float
    """The ``CDELT3`` fitscube will derive from the predicted frequencies, in Hz"""
    n_planes: int
    """The ``NAXIS3`` of the cube fitscube will create"""
    n_blank_planes: int
    """Number of planes in that cube with no image, written as NaN"""

    @property
    def padding(self) -> float:
        """Ratio of cube planes to images"""
        return self.n_planes / self.channels_out


def channel_grid_indices(frequencies: np.ndarray, channel_width: float) -> np.ndarray:
    """Index each channel centre on the native channel grid.

    Args:
        frequencies (np.ndarray): Sorted unique channel centres, in Hz
        channel_width (float): Native channel width, in Hz

    Returns:
        np.ndarray: Integer grid index of each channel

    Raises:
        ValueError: If the channels do not all share a single grid
    """
    offsets = (frequencies - frequencies[0]) / channel_width
    indices = np.round(offsets)
    if not np.allclose(offsets, indices, atol=1e-3, rtol=0):
        msg = f"Channel centres do not lie on a single grid of {channel_width=}"
        raise ValueError(msg)

    return indices.astype(np.int64)


def _partition(
    indices: np.ndarray,
    sizes: tuple[int, ...],
    t: int,
    phase: int,
) -> list[tuple[int, int]] | None:
    """Find contiguous bins covering all channels whose centres share a grid.

    Args:
        indices (np.ndarray): Native grid index of each channel
        sizes (tuple[int, ...]): Allowed bin sizes, most preferred first
        t (int): Grid step, in units of half the channel width
        phase (int): Required ``(q[a] + q[b]) % t`` of every bin

    Returns:
        list[tuple[int, int]] | None: Inclusive (start, end) channel indices of
        each bin, or None if no such partition exists
    """
    n = len(indices)

    # NOTE: a state is the *start* of a bin, and the transition marks the start
    # of the next bin. Marking bin ends instead makes everything look infeasible.
    came_from: dict[int, int] = {0: -1}
    for start in range(n):
        if start not in came_from:
            continue
        for size in sizes:
            end = start + size - 1
            if end >= n:
                continue
            if (indices[start] + indices[end]) % t != phase:
                continue
            if end + 1 not in came_from:
                came_from[end + 1] = start

    if n not in came_from:
        return None

    bins = []
    cursor = n
    while cursor > 0:
        start = came_from[cursor]
        bins.append((start, cursor - 1))
        cursor = start

    return bins[::-1]


def _describe(
    bins: list[tuple[int, int]], frequencies: np.ndarray, channel_width: float
) -> ChannelDivision:
    """Build the division and the cube fitscube will make from it."""
    centres = np.array([(frequencies[a] + frequencies[b]) / 2 for a, b in bins])
    if len(centres) > 1:
        new_specs, missing = even_spacing(centres * u.Hz)
        n_planes, n_blank = len(new_specs), int(np.sum(missing))
        cdelt3 = float(np.diff(new_specs.to(u.Hz).value)[0])
    else:
        n_planes, n_blank, cdelt3 = 1, 0, 0.0

    return ChannelDivision(
        channels_out=len(bins),
        # A channel joins a group by centre comparison, so split half a channel
        # below the first channel of each group
        channel_division_frequencies=tuple(
            float(frequencies[a] - channel_width / 2) for a, _ in bins[1:]
        ),
        predicted_frequencies=tuple(float(centre) for centre in centres),
        bin_channels=tuple(b - a + 1 for a, b in bins),
        cdelt3=cdelt3,
        n_planes=n_planes,
        n_blank_planes=n_blank,
    )


def _rank(division: ChannelDivision, target_size: float) -> tuple[float, int, float]:
    """Padding, then bin width spread, then distance from the requested width."""
    widths = division.bin_channels
    mean_width = sum(widths) / len(widths)

    return (
        round(division.padding, 2),
        max(widths) - min(widths),
        abs(mean_width - target_size),
    )


def solve_channel_division(
    frequencies: np.ndarray,
    channel_width: float | None = None,
    target_width: float | None = None,
    channels_out: int | None = None,
    size_tolerance: float = 0.25,
) -> ChannelDivision:
    """Solve for a wsclean channel division that yields a single compact cube.

    Args:
        frequencies (np.ndarray): Channel centres, the union across all MSs, in Hz
        channel_width (float | None, optional): Native channel width in Hz. Inferred from the centres if None. Defaults to None.
        target_width (float | None, optional): Desired output image width, in Hz. Defaults to None.
        channels_out (int | None, optional): Desired number of output images. Defaults to None.
        size_tolerance (float, optional): Fractional deviation from the target bin size that is allowed. Defaults to 0.25.

    Returns:
        ChannelDivision: The division and the cube it is predicted to produce

    Raises:
        ValueError: If neither or both targets are given, or no valid division exists
    """
    if (target_width is None) == (channels_out is None):
        msg = "Exactly one of target_width and channels_out is required"
        raise ValueError(msg)

    frequencies = np.unique(np.asarray(frequencies, dtype=float))
    if channel_width is None:
        channel_width = float(np.min(np.diff(frequencies)))

    indices = channel_grid_indices(frequencies=frequencies, channel_width=channel_width)
    n_channels = len(frequencies)

    target_size = (
        target_width / channel_width
        if target_width is not None
        else n_channels / channels_out  # type: ignore[operator]
    )
    min_size = max(1, int(np.floor(target_size * (1 - size_tolerance))))
    max_size = max(min_size, int(np.ceil(target_size * (1 + size_tolerance))))
    # Witnesses prefer sizes closest to the target, keeping the width spread small
    sizes = tuple(
        sorted(range(min_size, max_size + 1), key=lambda size: abs(size - target_size))
    )

    # t is a witness generator only: the ranking below decides, since the widest
    # step does not necessarily give the least padded or most uniform cube
    candidates: list[ChannelDivision] = []
    for t in range(2 * max_size, 0, -1):
        # Only phases reachable from the first bin can work
        phases = {
            int((indices[0] + indices[size - 1]) % t)
            for size in sizes
            if size <= n_channels
        }
        for phase in sorted(phases):
            bins = _partition(indices, sizes, t, phase)
            if bins is not None:
                candidates.append(
                    _describe(
                        bins=bins,
                        frequencies=frequencies,
                        channel_width=channel_width,
                    )
                )

    if not candidates:
        msg = (
            f"No valid channel division for {n_channels} channels with bin sizes "
            f"{min_size}-{max_size}. Try a different target or a larger size_tolerance."
        )
        raise ChannelDivisionError(msg)

    division = min(candidates, key=lambda option: _rank(option, target_size))
    logger.info(
        f"Solved channel division: {division.channels_out} images of "
        f"{sorted(set(division.bin_channels))} channels, {division.n_planes} planes "
        f"({division.n_blank_planes} blank), padding {division.padding:.2f}x"
    )

    # No single linear axis cube can beat span / bandwidth
    floor = (indices[-1] + 1) / n_channels
    if division.padding > MAX_PADDING_FACTOR * floor:
        msg = (
            f"Cube would be padded {division.padding:.2f}x against a floor of "
            f"{floor:.2f}x. Try another target width or a larger size_tolerance."
        )
        raise ChannelDivisionError(msg)

    return division


def channel_division_for_beams(
    mss_by_beam: Sequence[Sequence[MS]],
    target_width: float | None = None,
    channels_out: int | None = None,
    size_tolerance: float = 0.25,
) -> ChannelDivision:
    """Solve the one channel division to be shared by every beam of a field.

    Beams are coadded into a single cube, so they must all be imaged onto the
    same output frequency grid.

    Args:
        mss_by_beam (Sequence[Sequence[MS]]): The measurement sets to image, grouped by beam
        target_width (float | None, optional): Desired output image width, in Hz. Defaults to None.
        channels_out (int | None, optional): Desired number of output images. Defaults to None.
        size_tolerance (float, optional): Fractional deviation from the target bin size that is allowed. Defaults to 0.25.

    Returns:
        ChannelDivision: The division and the cube it is predicted to produce

    Raises:
        ChannelDivisionError: If the beams do not share a channel list
    """
    from flint.ms import get_freqs_from_ms

    beam_frequencies = [
        np.unique(np.concatenate([get_freqs_from_ms(ms) for ms in mss]))
        for mss in mss_by_beam
    ]
    for beam, frequencies in enumerate(beam_frequencies[1:], start=1):
        if frequencies.shape != beam_frequencies[0].shape or not np.allclose(
            frequencies, beam_frequencies[0]
        ):
            msg = (
                f"Beam {beam} does not share the channel list of beam 0, so beams "
                "cannot share a single cube frequency grid"
            )
            raise ChannelDivisionError(msg)

    return solve_channel_division(
        frequencies=beam_frequencies[0],
        target_width=target_width,
        channels_out=channels_out,
        size_tolerance=size_tolerance,
    )
