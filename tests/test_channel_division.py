"""Testing the wsclean output channel division solver.

wsclean's division is mocked rather than run: ``mock_wsclean_division`` mirrors
``WSClean::makeImagingTableEntryChannelSettings`` (both the default and the
``-channel-division-frequencies`` group path, integer division included) and
``io/wscfitswriter.cpp``'s ``CRVAL3``.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pytest
from astropy import units as u
from fitscube.combine_fits import even_spacing

from flint.exceptions import ChannelDivisionError
from flint.imager.channel_division import (
    ChannelDivision,
    channel_grid_indices,
    solve_channel_division,
)
from flint.imager.wsclean import (
    WSCleanOptions,
    _resolve_wsclean_key_value_to_cli_str,
)
from flint.prefect.flows.racs_all_continuum_selfcal import _apply_cube_division


def mock_wsclean_division(
    channels: np.ndarray,
    channels_out: int,
    division_frequencies: Sequence[float] | None = None,
) -> np.ndarray:
    """Predicted ``CRVAL3`` of each wsclean output image, in Hz."""
    groups: list[np.ndarray] = []
    group_channels_out: list[int] = []
    if division_frequencies is None:
        groups = [channels]
        group_channels_out = [channels_out]
    else:
        n_splits = len(division_frequencies)
        edges = [0.0, *division_frequencies, np.inf]
        for index in range(n_splits + 1):
            low, high = edges[index], edges[index + 1]
            groups.append(channels[(channels >= low) & (channels < high)])
            group_channels_out.append(
                channels_out * (index + 1) // (n_splits + 1)
                - channels_out * index // (n_splits + 1)
            )

    centres: list[float] = []
    for group, n_out in zip(groups, group_channels_out):
        if n_out == 0 or len(group) == 0:
            msg = "Too many output channels requested"
            raise ValueError(msg)
        for out_index in range(n_out):
            low_index = out_index * len(group) // n_out
            high_index = (out_index + 1) * len(group) // n_out - 1
            centres.append((group[low_index] + group[high_index]) / 2)

    return np.array(centres)


@pytest.fixture
def channels() -> np.ndarray:
    """The Part 4 reference channel list: 720 x 1MHz channels over a 1000MHz span."""
    return np.concatenate(
        [
            799990740.7 + 1e6 * np.arange(288),
            1295990740.7 + 1e6 * np.arange(144),
            1511990740.7 + 1e6 * np.arange(288),
        ]
    )


def cube_shape(centres: np.ndarray) -> tuple[int, int, float]:
    """Planes, blank planes and CDELT3 that fitscube will produce."""
    new_specs, missing = even_spacing(centres * u.Hz)
    return len(new_specs), int(np.sum(missing)), float(np.diff(new_specs.value)[0])


def test_channel_grid_indices(channels: np.ndarray) -> None:
    indices = channel_grid_indices(frequencies=channels, channel_width=1e6)

    assert indices[0] == 0
    assert indices[288] == 496
    assert indices[288 + 144] == 712

    with pytest.raises(ValueError, match="single grid"):
        channel_grid_indices(
            frequencies=np.array([1e9, 1e9 + 1.5e6]), channel_width=1e6
        )


def test_mock_default_division_regression(channels: np.ndarray) -> None:
    """The observed 48 image, 20.5x padded failure the solver exists to avoid."""
    centres = mock_wsclean_division(channels=channels, channels_out=48)

    assert len(centres) == 48
    n_planes, n_blank, cdelt3 = cube_shape(centres)
    assert (n_planes, n_blank) == (986, 938)
    assert cdelt3 == pytest.approx(1e6)


def test_solve_8mhz_regression(channels: np.ndarray) -> None:
    """The Part 4 pick: 90 images of exactly 8 channels, on the padding floor."""
    division = solve_channel_division(frequencies=channels, target_width=8e6)

    assert division.channels_out == 90
    assert set(division.bin_channels) == {8}
    assert division.cdelt3 == pytest.approx(8e6)
    assert (division.n_planes, division.n_blank_planes) == (125, 35)
    assert division.padding == pytest.approx(1000 / 720, abs=0.01)
    assert len(division.channel_division_frequencies) == 89
    assert division.channel_division_frequencies[0] == pytest.approx(807490740.7)


@pytest.mark.parametrize(
    ("target_width", "expect"),
    [(11.5e6, (63, 87, 24, 11.5e6)), (17e6, (43, 59, 16, 17e6))],
)
def test_solve_coarser_regressions(
    channels: np.ndarray, target_width: float, expect: tuple[int, int, int, float]
) -> None:
    division = solve_channel_division(frequencies=channels, target_width=target_width)

    assert (
        division.channels_out,
        division.n_planes,
        division.n_blank_planes,
    ) == expect[:3]
    assert division.cdelt3 == pytest.approx(expect[3])


def test_solve_round_trip_through_mock(channels: np.ndarray) -> None:
    """wsclean, given the solved division, produces the predicted centres."""
    for target_width in (8e6, 12e6, 17e6, 20e6):
        division = solve_channel_division(
            frequencies=channels, target_width=target_width
        )
        centres = mock_wsclean_division(
            channels=channels,
            channels_out=division.channels_out,
            division_frequencies=division.channel_division_frequencies,
        )

        assert np.allclose(centres, division.predicted_frequencies)
        assert np.all(np.diff(centres) > 0)
        assert sum(division.bin_channels) == len(channels)
        assert min(division.bin_channels) > 0
        assert cube_shape(centres) == (
            division.n_planes,
            division.n_blank_planes,
            pytest.approx(division.cdelt3),
        )
        # Padding floor of a single linear axis cube is span / bandwidth
        assert division.padding < 1.45


def test_solve_channels_out_target(channels: np.ndarray) -> None:
    division = solve_channel_division(frequencies=channels, channels_out=90)

    assert division.channels_out == 90
    assert set(division.bin_channels) == {8}


def test_solve_uniform_band(channels: np.ndarray) -> None:
    """No gaps means the requested division is simply honoured."""
    uniform = 1e9 + 1e6 * np.arange(100)
    division = solve_channel_division(frequencies=uniform, target_width=10e6)

    assert division.channels_out == 10
    assert set(division.bin_channels) == {10}
    assert (division.n_planes, division.n_blank_planes) == (10, 0)


def test_solve_requires_one_target(channels: np.ndarray) -> None:
    with pytest.raises(ValueError, match="Exactly one"):
        solve_channel_division(frequencies=channels)
    with pytest.raises(ValueError, match="Exactly one"):
        solve_channel_division(frequencies=channels, target_width=8e6, channels_out=90)


def test_solve_infeasible() -> None:
    with pytest.raises(ChannelDivisionError, match="No valid channel division"):
        solve_channel_division(
            frequencies=1e9 + 1e6 * np.arange(10),
            target_width=3e6,
            size_tolerance=0.0,
        )


def test_solve_rejects_padded_cube(channels: np.ndarray) -> None:
    """A target with no compact solution fails, rather than imaging for hours first."""
    with pytest.raises(ChannelDivisionError, match="padded"):
        solve_channel_division(frequencies=channels, target_width=24e6)


def test_apply_cube_division() -> None:
    division = ChannelDivision(
        channels_out=3,
        channel_division_frequencies=(1e9, 2e9),
        predicted_frequencies=(0.0, 0.0, 0.0),
        bin_channels=(1, 1, 1),
        cdelt3=1.0,
        n_planes=3,
        n_blank_planes=0,
    )

    updated = _apply_cube_division(
        update_wsclean_options={"channels_out": 16, "niter": 10},
        cube_division=division,
    )
    assert updated == {
        "channels_out": 3,
        "niter": 10,
        "channel_division_frequencies": (1e9, 2e9),
    }

    # An explicit division in the strategy is left alone
    pinned = {"channels_out": 16, "channel_division_frequencies": (3e9,)}
    assert (
        _apply_cube_division(update_wsclean_options=pinned, cube_division=division)
        == pinned
    )


def test_division_frequencies_to_cli() -> None:
    division = ChannelDivision(
        channels_out=3,
        channel_division_frequencies=(1e9, 2e9),
        predicted_frequencies=(0.0, 0.0, 0.0),
        bin_channels=(1, 1, 1),
        cdelt3=1.0,
        n_planes=3,
        n_blank_planes=0,
    )
    options = WSCleanOptions(
        channels_out=division.channels_out,
        channel_division_frequencies=division.channel_division_frequencies,
    )
    result = _resolve_wsclean_key_value_to_cli_str(
        "channel_division_frequencies", options.channel_division_frequencies
    )

    assert result.cmd == "-channel-division-frequencies 1000000000.0,2000000000.0"
