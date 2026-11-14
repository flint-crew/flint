# Cublet imaging (experimental)

`flint_flow_beam_cublet_pipeline` cleans one beam island by island. It is an
experiment, and images a single measurement set.

The reasoning behind it is in [Why cublet imaging works the way it does](cublet_design.md).

1. A wide MFS image with few channels is made and masked with
   [eye-patch](masking.md).
2. The mask is split into islands. Each gets a square window, its own clean
   mask, and a shift to its centre.
3. Each island is imaged on its own, in a small image with many channels, and
   its clean components are saved.
4. The new components of all islands are predicted with `crystalball` and
   subtracted from the data.
5. Steps 3 and 4 repeat, each round cleaning deeper.
6. With more than one cycle, steps 1 to 5 repeat on what is left of the data.
7. A final image of what is left is made, and all the components found are
   restored onto its MFS image.

Islands only meet in the visibilities, through the sidelobes they put on each
other. A round stops every island at the same level,
`peak * (1 - round_gain) ** round`, so the rounds are the major cycles.

The clean mask of an island keeps its components on real emission, so an island
can not mistake the sidelobe of another for a source. At worst the sidelobe is
taken into its own flux, which the next round corrects once the other island
has been subtracted. Rounds can therefore be deep. Stopping a round near the
far sidelobe level of the PSF, below the brightest island, is as deep as is
useful: the default `round_gain` of 0.97 with three rounds reaches 0.003% of
the brightest island. Bright islands are right after the first round, and
faint ones after the second.

An island is left out of a round when nothing above the level of the round is
left inside its mask. What is left is measured as an absolute value, so an
island that was cleaned too far counts as unfinished.

## Cycles

`--cycles` sets how many times the whole beam is imaged to find islands. A
later cycle images the data less the components found so far, and masks that
image. With the bright islands gone the mask reaches fainter emission.

The mask of a cycle is joined with the masks of the cycles before it, and the
islands are found again on the result. An island of an earlier cycle is
therefore never closed. It is imaged again whenever something is left inside
its mask, be it positive or negative, and it is one island with any new
emission that touches it.

Each island image starts from an empty model, so what a later cycle finds for
an island is a correction to its earlier components, and may be negative.

The first cycle has `--rounds` rounds and each later cycle `--later-rounds`,
which is 1 by default. Faint islands hardly put sidelobes on each other, so
more are seldom needed. The cycles stop early when one finds no islands, or
no components.

Set `--threshold-floor`, in Jy, when using cycles. An island with nothing
above it left inside its mask is not imaged, and without it every island of
every cycle is imaged in every round. It only stops the cleaning. Where
components may go is set by the masks alone.

## Strategy

Two operations are read from the strategy file: `cubletimage` for the MFS image
and its mask, and `cubletisland` for the island images. Both are keyed by
cycle, as `selfcal` is keyed by round. Each cycle is laid over `defaults` on
its own, so a later cycle repeats what it shares with an earlier one. A cycle
past the last one given uses the last.

```yaml
version: 0.2

defaults:
  wsclean:
    scale: 2.5asec
    weight: briggs 0.5

cubletimage:
  1:
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
      seed_clip: 1.5
      multiscale_scales: [0, 8, 16, 32]
  2:
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
      seed_clip: 1.2
      multiscale_scales: [0, 8, 16, 32]

cubletisland:
  1:
    wsclean:
      channels_out: 72
      fit_spectral_pol: 3
      gain: 0.7
      niter: 1000
      multiscale: true
      multiscale_scales: [0, 8, 16, 32]
      auto_mask: null
      auto_threshold: 0.75
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
- wsclean stops at the higher of `auto_threshold` and the level of the round,
  so the noise sets the depth once the rounds have gone below it.
- A robust mask allows a high `gain` with few iterations. That also keeps the
  component lists, and so their prediction, small.
- With a per-scale mask the `multiscale_scales` of the mask and of both wsclean
  blocks should match.
- The `size` and `scale` of the MFS image have to be the same in every cycle,
  as the masks of the cycles are joined pixel by pixel.

## Reordered visibilities

By default the first island of a round reorders
the visibilities, and the other islands of the round share its files. This
needs a `--work-dir` that every worker can see. Do not point it at node-local
storage. With `--no-reuse-reordered` every island reorders for itself, which
an older wsclean without the reorder options needs.

The island masks are kept in the `--work-dir` too, so it also has to be bound
into the wsclean container. The default, a `cublet` folder beside the
measurement set, already is.

## Predicting

`crystalball` splits its work into chunks of rows and of components, and only
chunks can be spread over workers. Left to itself it tends to put a whole beam
in one chunk, which a single worker then predicts. `--row-chunks` and
`--model-chunks` set the sizes, and have to be given together.

## Stalls

With `--watch-cluster` the flow logs once a minute what the dask scheduler holds:
the state of its tasks, those yet to start, what each worker is running and
when it was last heard from, and where each running task is in its code.

## Outputs

- The component list of each island, named with a `c<cycle>r<round>i<island>`
  suffix, beside the measurement set. An island keeps its number only within a
  cycle. The FITS images of an island are deleted once it has been imaged. Give
  `--keep-island-images` to keep them, named the same way.
- The MFS image of each cycle. Those of later cycles have a `cycle<N>` suffix.
- `cublet/cycle<N>/masks`, the clean masks of each island, and
  `cublet/cycle<N>`, the joined mask of the cycle.
- `cublet/*.c<cycle>r<round>-sources.txt`, the components found in that round,
  and `cublet/*.to-c<cycle>r<round>-sources.txt`, all components up to it.
- The `--residual-column` of the measurement set, or the column with an `_ALT`
  suffix. Subtractions alternate between the two, so that a rerun of one can
  not subtract twice. The log of the last subtraction names the column.
- An image of what is left, with the `cubletresidual` suffix.
- The MFS image of what is left with every component found restored onto it,
  with the `cubletrestored` suffix. wsclean does this with `-restore-list`,
  using the beam recorded in the image. It is not corrected for the primary
  beam.
