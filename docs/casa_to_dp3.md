# Moving self-calibration from CASA to DP3

A self-calibration round solves with DP3 instead of CASA `gaincal` when that round of the [strategy file](config.md) has a `dp3gaincal` or `dp3ddecal` block (see {ref}`Self-calibration with DP3 <dp3-selfcal>`). This page maps each `gaincal` option to its DP3 equivalent. Both DP3 solvers take their settings in DP3's own names and units.

## Option by option

| CASA `gaincal` | `dp3gaincal` | `dp3ddecal` | Notes |
| --- | --- | --- | --- |
| `calmode` and `gaintype` | `caltype` | `mode` | See [solve type](#casa-to-dp3-solve-type) |
| `solint: 60s` | `solint: 6` | `solint: 6` | In timeslots. ASKAP integrates for about 10 s. `inf` is `0`. |
| `nspw: 4` | `nchan: 72` | `nchan: 72` | Channels per solution, so 288 channels / 4. `0` is the whole band. |
| `uvrange: ">200m"` | `uvmmin: 200` | `uvmmin: 200` | `"<5000m"` is `uvmmax: 5000`. For `klambda` ranges use `uvlambdamin`. |
| `solnorm: true` | `dp3selfcal: {normalise_amplitudes: true}` | the same | On by default. Scales amplitudes to a mean of one. |
| `minsnr` | none | none | DP3 does not flag low signal-to-noise solutions. Flint's default of `0.0` already turns this off. |
| `selectdata`, `round` | none | none | DP3 always applies its uv cuts, and needs no round number. |

`dp3ddecal` can also smooth solutions across frequency rather than solving channel blocks on their own: use `nchan: 1` with `smoothnessconstraint` in Hz. This is not an exact match for `nspw`.

(casa-to-dp3-solve-type)=
## Solve type

`gaintype: G`, flint's default, solves a gain for each polarisation. `gaintype: T` solves one gain shared by both. The solve type keeps the same name in `dp3gaincal` (`caltype`) and `dp3ddecal` (`mode`):

| `gaintype` | `calmode: p` | `calmode: a` | `calmode: ap` |
| --- | --- | --- | --- |
| `G` | `diagonalphase` | `diagonalamplitude` | `diagonal` |
| `T` | `scalarphase` | `scalaramplitude` | `scalar` |

The [sky-model calibration](predict/skymodel.md) in round 0 differs. With CASA it solves `calmode: p` per polarisation, but with DP3 it always solves `scalarphase`.

## A worked example

These are flint's usual CASA rounds:

```yaml
selfcal:
  1: {gaincal: {solint: 60s, uvrange: ">235m", nspw: 1}}
  2: {gaincal: {solint: 30s, calmode: p, uvrange: ">235m", nspw: 4}}
  3: {gaincal: {solint: 60s, calmode: ap, uvrange: ">235m", nspw: 4}}
  4: {gaincal: {solint: 30s, calmode: ap, uvrange: ">235m", nspw: 4}}
```

The same rounds with DP3, for 288 channels. The shared uv cut moves to `defaults`, but each round still needs its own `dp3gaincal` block, since that is what selects DP3:

```yaml
defaults:
  dp3gaincal: {uvmmin: 235}
selfcal:
  1: {dp3gaincal: {caltype: diagonalphase, solint: 6, nchan: 0}}
  2: {dp3gaincal: {caltype: diagonalphase, solint: 3, nchan: 72}}
  3: {dp3gaincal: {caltype: diagonal, solint: 6, nchan: 72}}
  4: {dp3gaincal: {caltype: diagonal, solint: 3, nchan: 72}}
```

## What else changes

- **Solutions:** each round writes an h5parm, `<ms>.roundN.h5`, in place of CASA's `.caltable` directories. Both are archived.
- **Containers:** the CASA container is needed only while some round, or the sky-model round, still uses `gaincal`. Preprocessing CASDA measurement sets still uses it.
- **Mixing:** rounds can use different solvers. A round that names more than one solver is an error.
