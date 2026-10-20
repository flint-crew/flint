# Creating Clean Masks

Clean masks restrict where CLEAN may place components. `flint` makes them with
[eye-patch](https://github.com/flint-crew/eye-patch), which began as the
`flint.masking` module. The
[eye-patch documentation](https://eye-patch.readthedocs.io/) describes the
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

## Renamed options

eye-patch 2.0 renamed most of the masking options. A strategy file that uses an
old name fails verification until it is renamed:

| Old name                                  | New name                         |
| ----------------------------------------- | -------------------------------- |
| `base_snr_clip`                           | `base_clip`                      |
| `flood_fill_positive_seed_clip`           | `seed_clip`                      |
| `flood_fill_positive_flood_clip`          | `flood_clip`                     |
| `flood_fill_use_mac`                      | `use_mac`                        |
| `flood_fill_use_mac_box_size`             | `mac_box_size`                   |
| `flood_fill_use_mac_adaptive_step_factor` | `mac_adaptive_step_factor`       |
| `flood_fill_use_mac_adaptive_skew_delta`  | `mac_adaptive_skew_delta`        |
| `flood_fill_use_mac_adaptive_max_depth`   | `mac_adaptive_max_depth`         |
| `grow_low_snr_island_size`                | `grow_low_snr_island_min_pixels` |
| `beam_shape_erode_scales`                 | `multiscale_scales`              |

Each of the `multiscale_scales` is now treated as a wsclean multi-scale scale,
which erodes less than earlier versions did. Set
`beam_shape_erode_scales_as_fwhm: true` to keep the old behaviour.
