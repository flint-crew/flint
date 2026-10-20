# Creating Clean Masks

Clean masks restrict where CLEAN may place components. `flint` makes them with
[eye-patch](https://github.com/flint-crew/eye-patch), which began as the
`flint.masking` module. The
[eye-patch documentation](https://eye-patch.readthedocs.io/en/stable/) describes the
signal-to-noise and minimum absolute clip statistics, flood filling, beam shape
erosion and the `convolve_first` approach.

## Setting masking options

Options in the `masking` mode of a strategy file are passed to
`eye_patch.options.MaskingOptions`. In the self-calibration flows they may be
set per round, like the `wsclean` and `gaincal` options (see
[the strategy file](config.md)).

Masks can also be made outside of a flow. `eye-patch mask` creates a mask for an
image, and `eye-patch extractmask` cuts the region of a beam out of a larger
mask. Run either with `-h` to list its options.

### Per-scale masks

`convolve_first`, and `beam_shape_erode` with more than one `multiscale_scales`,
make a per-scale mask. In a round where wsclean cleans with `multiscale` it is
passed as wsclean's per-scale mask. In any other round it is passed as a plain
clean mask, where every non-zero pixel may be cleaned.

wsclean reads bit _n_ of each pixel as its _n_'th smallest scale, and does not
clean at a scale that has no pixels set. In multiscale rounds the masking
`multiscale_scales` must therefore match that round's wsclean
`multiscale_scales`. `flint` does not check this yet.

## Upgrading strategy files

eye-patch 2.0 renamed most of the masking options and changed what some of them
do. A strategy file that uses an old name fails verification. eye-patch's
[migration notes](https://eye-patch.readthedocs.io/en/stable/masking.html#changes-from-v1-0-1-and-earlier-versions)
map each old name to its new one, and list the changes in behaviour. In
particular, settings that use `use_mac` or `grow_low_snr_island` without
`flood_fill` now make a different mask, and should be revisited.
