# Cublet imaging (experimental)

`flint_flow_beam_cublet_pipeline` cleans one beam island by island. It is an
experiment, and images a single measurement set.

1. A wide MFS image with few channels is made and masked with
   [eye-patch](masking.md).
2. The mask is split into islands. Each gets a square window, its own clean
   mask, and a shift to its centre.
3. Each island is imaged on its own, in a small image with many channels, and
   its clean components are saved.
4. The new components of all islands are predicted with `crystalball` and
   subtracted from the data.
5. Steps 3 and 4 repeat, each round cleaning deeper. A final image of what is
   left is made.

Islands only meet in the visibilities. A round stops every island at the same
level, `peak * (1 - round_gain) ** round`, so that none cleans the sidelobes of
a brighter island that has not been subtracted yet. The rounds are therefore
the major cycles, and `--rounds` sets the depth: the defaults of two rounds and
a gain of 0.8 stop at 4% of the brightest island.

An island is left out of a round when nothing above the level of the round is
left inside its mask.

## Strategy

Two operations are read from the strategy file: `cubletimage` for the MFS image
and its mask, and `cubletisland` for the island images.

```yaml
version: 0.2

defaults:
  wsclean:
    scale: 2.5asec
    weight: briggs 0.5

cubletimage:
  wsclean:
    size: 8144
    channels_out: 4
    fit_spectral_pol: 2
    multiscale: true
    multiscale_scales: [0, 8, 16, 32]
  masking:
    use_mac: true
    flood_fill: true
    beam_shape_erode: true
    multiscale_scales: [0, 8, 16, 32]

cubletisland:
  wsclean:
    channels_out: 72
    fit_spectral_pol: 3
    multiscale: true
    multiscale_scales: [0, 8, 16, 32]
    auto_mask: null
    auto_threshold: null
    local_rms: false
```

Notes on the island options:

- The flow sets `size`, `shift`, `super_weight`, `threshold`, the mask and the
  temporary directory of each island. `super_weight` is the padded island size
  over the padded MFS image size, which gives the island the weights of the MFS
  image. The default wsclean padding of 1.2 is assumed.
- `mgain` defaults to 1, as the rounds are the major cycles. Each island is then
  one pass for its PSF and one for its image. Set `mgain` to have wsclean run
  major cycles of its own within a round.
- `fit_spectral_pol` is needed, as wsclean saves components as polynomials.
- The clean mask is the island, so auto-masking is turned off.
- With a per-scale mask the `multiscale_scales` of the mask and of both wsclean
  blocks should match.

## Reordered visibilities

With `--reuse-reordered` (the default) the brightest island of a round reorders
the visibilities, and the other islands of the round share its files. This
needs a `--work-dir` that every worker can see. Do not point it at node-local
storage.

The island masks are kept in the `--work-dir` too, so it also has to be bound
into the wsclean container. The default, a `cublet` folder beside the
measurement set, already is.

## Outputs

- Island images, named with a `r<round>i<island>` suffix, beside the
  measurement set.
- `cublet/masks`, the clean masks of each island.
- `cublet/*.round<N>-sources.txt`, the components found in that round, and
  `cublet/*.rounds1to<N>-sources.txt`, all components up to it.
- The `--residual-column` of the measurement set, or the column with an `_ALT`
  suffix after an even number of rounds. Rounds alternate between the two, so
  that a rerun of a subtraction can not subtract twice.
- An image of what is left, with the `cubletresidual` suffix.
