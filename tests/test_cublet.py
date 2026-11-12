"""Tests of the island tooling behind the beam cublet flow"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS
from eye_patch.naming import FITSMaskNames

from flint.cublet.islands import (
    create_cublet_regions,
    find_cublet_regions,
    island_cutout,
    label_islands,
    round_threshold,
)
from flint.cublet.reordered import link_reordered_files
from flint.cublet.sourcelist import concatenate_source_lists

SIZE = 400


@pytest.fixture
def header() -> fits.Header:
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---SIN", "DEC--SIN"]
    wcs.wcs.crval = [180.0, -30.0]
    wcs.wcs.crpix = [SIZE / 2 + 1, SIZE / 2 + 1]
    wcs.wcs.cdelt = [-2.5 / 3600, 2.5 / 3600]
    return wcs.to_header()


@pytest.fixture
def mask() -> np.ndarray:
    """Three islands. The second and third are five pixels apart."""
    mask = np.zeros((SIZE, SIZE), dtype=np.float32)
    mask[50:60, 100:130] = 1
    mask[300:310, 200:210] = 1
    mask[300:310, 215:220] = 1
    return mask


@pytest.fixture
def image(mask) -> np.ndarray:
    image = np.zeros_like(mask)
    image[55, 110] = 1.0
    image[305, 205] = 5.0
    image[305, 217] = 3.0
    return image


def test_round_threshold() -> None:
    """Each round cleans a set fraction deeper, down to the floor"""
    assert round_threshold(peak=10.0, round_gain=0.8, round_number=1) == pytest.approx(
        2.0
    )
    assert round_threshold(peak=10.0, round_gain=0.8, round_number=2) == pytest.approx(
        0.4
    )
    assert round_threshold(peak=10.0, round_gain=0.8, round_number=2, floor=1.0) == 1.0


def test_label_islands(mask) -> None:
    """Islands are labelled apart unless they are close enough to merge"""
    assert label_islands(mask=mask).max() == 3

    merged = label_islands(mask=mask, merge_pixels=3)
    assert merged.max() == 2
    assert merged[305, 205] == merged[305, 217]
    # Merging does not grow the islands
    assert np.array_equal(merged > 0, mask > 0)


def test_label_islands_scale_mask(mask) -> None:
    """Any scale bit makes a pixel part of an island"""
    assert label_islands(mask=mask * 6).max() == 3


def test_create_cublet_regions(mask, image, header) -> None:
    """Windows are even, padded, sorted by peak and centred on a pixel"""
    labels = label_islands(mask=mask)
    regions = create_cublet_regions(
        labels=labels, image=image, header=header, pad_factor=2.0, min_size=16
    )

    assert [region.peak for region in regions] == [5.0, 3.0, 1.0]
    assert all(region.size % 2 == 0 for region in regions)

    faint = regions[-1]
    assert faint.centre == (55, 115)
    assert faint.size == 60
    assert faint.super_weight == pytest.approx(60 / SIZE)

    # The shift is the sky position of the centre pixel
    position = SkyCoord(*faint.shift)
    x, y = WCS(header).world_to_pixel(position)
    assert (x, y) == pytest.approx((115, 55), abs=1e-3)


def test_create_cublet_regions_limits(mask, image, header) -> None:
    """Small islands get the smallest window, and only the brightest are kept"""
    labels = label_islands(mask=mask)
    regions = create_cublet_regions(
        labels=labels, image=image, header=header, min_size=128, max_islands=2
    )
    assert [region.peak for region in regions] == [5.0, 3.0]
    assert all(region.size == 128 for region in regions)


def test_create_cublet_regions_empty(mask, image, header) -> None:
    """No islands, no regions"""
    labels = np.zeros_like(mask, dtype=np.int32)
    assert create_cublet_regions(labels=labels, image=image, header=header) == []


def test_island_cutout_other_islands(mask, image, header) -> None:
    """A window only holds its own island, with its scale bits"""
    labels = label_islands(mask=mask)
    regions = create_cublet_regions(
        labels=labels, image=image, header=header, pad_factor=4.0, min_size=16
    )
    brightest = regions[0]
    cutout = island_cutout(mask=mask * 6, labels=labels, region=brightest)

    assert cutout.shape == (brightest.size, brightest.size)
    # The neighbouring island is inside the window, and is left out
    assert np.sum(cutout > 0) == 100
    assert set(np.unique(cutout)) == {0, 6}
    # The centre of the window is the centre pixel of the region
    half = brightest.size // 2
    assert cutout[half, half] == 6


def test_island_cutout_past_edge(header) -> None:
    """A window that runs off the image is padded with zeros"""
    mask = np.zeros((SIZE, SIZE), dtype=np.float32)
    mask[0:4, 0:4] = 1
    image = mask.copy()
    labels = label_islands(mask=mask)
    (region,) = create_cublet_regions(
        labels=labels, image=image, header=header, min_size=32
    )
    cutout = island_cutout(mask=mask, labels=labels, region=region)

    assert cutout.shape == (32, 32)
    assert np.sum(cutout) == 16
    assert cutout[16, 16] == mask[region.centre]


def test_find_cublet_regions(mask, image, header, tmp_path) -> None:
    """Island masks are written on the pixel grid of the image"""
    image_fits = tmp_path / "field.fits"
    scale_mask_fits = tmp_path / "field.scalemask.fits"
    fits.writeto(image_fits, data=image[None, None], header=header)
    fits.writeto(scale_mask_fits, data=(mask * 6)[None, None], header=header)

    regions = find_cublet_regions(
        image_fits=image_fits,
        mask_names=FITSMaskNames(scale_mask_fits=scale_mask_fits),
        output_dir=tmp_path / "masks",
        min_size=32,
    )
    assert len(regions) == 3

    for region in regions:
        assert region.fits_mask is not None
        assert region.fits_mask.mask_fits is None
        island_mask_fits = region.fits_mask.scale_mask_fits
        assert island_mask_fits.name == f"field.island{region.idx:05d}.scalemask.fits"

        with fits.open(island_mask_fits) as island_mask:
            assert island_mask[0].data.shape == (region.size, region.size)
            assert island_mask[0].data.max() == 6
            island_wcs = WCS(island_mask[0].header)

        # The centre of the window is where the centre pixel is in the image
        half = region.size // 2
        position = island_wcs.pixel_to_world(half, half)
        x, y = WCS(header).world_to_pixel(position)
        assert (y, x) == pytest.approx(region.centre, abs=1e-6)


SOURCE_LIST_HEADER = (
    "Format = Name, Type, Ra, Dec, I, SpectralIndex, LogarithmicSI, "
    "ReferenceFrequency='943490740.740741', MajorAxis, MinorAxis, Orientation"
)


def _write_source_list(path: Path, components: int, header: str = SOURCE_LIST_HEADER):
    lines = [header] + [
        f"s0c{index},POINT,08:28:05.152,-39.35.08.511,0.1,[0.0,0.0],false,943490740.740741,,,"
        for index in range(components)
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_concatenate_source_lists(tmp_path) -> None:
    """Components are joined under one header, with their names made unique"""
    first = _write_source_list(tmp_path / "a-sources.txt", components=2)
    second = _write_source_list(tmp_path / "b-sources.txt", components=1)
    empty = _write_source_list(tmp_path / "c-sources.txt", components=0)
    missing = tmp_path / "d-sources.txt"

    output = concatenate_source_lists(
        source_lists=[first, second, empty, missing],
        tags=["r1i00001", "r1i00002", "r1i00003", "r1i00004"],
        output=tmp_path / "all-sources.txt",
    )
    assert output is not None

    lines = output.read_text().splitlines()
    assert lines[0] == SOURCE_LIST_HEADER
    assert [line.split(",")[0] for line in lines[1:]] == [
        "r1i00001s0c0",
        "r1i00001s0c1",
        "r1i00002s0c0",
    ]

    # An earlier joined list is added to without renaming its components
    again = concatenate_source_lists(
        source_lists=[output, second],
        tags=["", "r2i00002"],
        output=tmp_path / "again-sources.txt",
    )
    assert again is not None
    assert len(again.read_text().splitlines()) == 5


def test_concatenate_source_lists_no_components(tmp_path) -> None:
    """Nothing is written when no list holds a component"""
    empty = _write_source_list(tmp_path / "a-sources.txt", components=0)
    output = tmp_path / "all-sources.txt"

    assert (
        concatenate_source_lists(source_lists=[empty], tags=["a"], output=output)
        is None
    )
    assert not output.exists()


def test_concatenate_source_lists_header_mismatch(tmp_path) -> None:
    """Lists made at different reference frequencies can not be joined"""
    first = _write_source_list(tmp_path / "a-sources.txt", components=1)
    second = _write_source_list(
        tmp_path / "b-sources.txt",
        components=1,
        header=SOURCE_LIST_HEADER.replace("943490740", "1367490740"),
    )
    with pytest.raises(ValueError, match="header"):
        concatenate_source_lists(
            source_lists=[first, second],
            tags=["a", "b"],
            output=tmp_path / "all-sources.txt",
        )


def test_link_reordered_files(tmp_path) -> None:
    """Data is shared with the seed, and the model is new and empty"""
    ms_path = tmp_path / "SB1234.beam00.ms"
    seed_dir = tmp_path / "round1"
    seed_dir.mkdir()
    names = [
        "SB1234.beam00.ms-part0000-I.tmp",
        "SB1234.beam00.ms-part0000-I-w.tmp",
        "SB1234.beam00.ms-part0000-I-m.tmp",
        "SB1234.beam00.ms-meta-0000.tmp",
    ]
    for name in names:
        (seed_dir / name).write_bytes(b"jack sparrow")
    (seed_dir / "OTHER.ms-part0000-I.tmp").write_bytes(b"not ours")

    target_dir = link_reordered_files(
        seed_dir=seed_dir, target_dir=seed_dir / "island00002", ms_path=ms_path
    )

    assert sorted(path.name for path in target_dir.iterdir()) == sorted(names)
    for name in names:
        seed, target = seed_dir / name, target_dir / name
        if name.endswith("-m.tmp"):
            assert not target.samefile(seed)
            assert target.read_bytes() == bytes(seed.stat().st_size)
        else:
            assert target.samefile(seed)

    # A rerun replaces what an earlier attempt left
    (target_dir / names[2]).write_bytes(b"stale model")
    link_reordered_files(seed_dir=seed_dir, target_dir=target_dir, ms_path=ms_path)
    assert (target_dir / names[2]).read_bytes() == bytes(len(b"jack sparrow"))


def test_link_reordered_files_missing(tmp_path) -> None:
    """Without reordered files there is nothing to share"""
    with pytest.raises(AssertionError, match="No reordered files"):
        link_reordered_files(
            seed_dir=tmp_path,
            target_dir=tmp_path / "island",
            ms_path=tmp_path / "SB1234.beam00.ms",
        )
