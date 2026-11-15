# DP3

`Flint` can run steps of [DP3](https://dp3.readthedocs.io/) against a measurement set. DP3 is installed from `pip` with the `dp3` extra (Linux x86_64 only):

```bash
pip install 'askap-flint[dp3]'
```

DP3 is driven through its Python interface, so no container is needed. Each task builds the DP3 settings in memory and makes a single pass over the measurement set, updating it in place.

## Tasks

The tasks live in `flint.dp3`:

- `dp3_flag`: flags with `aoflagger`, using the packaged `ASKAP.lua` strategy unless another is given. A `preflagger` step can run first.
- `dp3_ddecal`: solves for gains against the model column with `ddecal`, and returns the measurement set with `h5parm` set to the solutions.
- `dp3_gaincal`: the same, with `gaincal`.
- `dp3_applycal`: applies one or more solution tables from an h5parm, writing the corrected data to a new column (`CORRECTED_DATA` by default). The measurement set's `h5parm` is used when none is given.
- `dp3_solve_apply`: solves and applies in one call. With `gaincal` this is a single pass over the data. With `ddecal` the solutions are written to an h5parm and then applied.

Calibration solves against the measurement set's `model_column` (e.g. `MODEL_DATA` written by `wsclean`), which must be set.

Each step has an options class (e.g. `DP3DDECalOptions`) whose fields are DP3 settings for that step. Unset options are left to DP3's defaults. They can also be set in a strategy file under the `dp3preflagger`, `dp3aoflagger`, `dp3ddecal`, `dp3gaincal` and `dp3applycal` modes.

## Self-calibration with DP3

A self-calibration round in the continuum flow solves with DP3 when that round of the [strategy file](config.md) has a `dp3ddecal` (or `dp3gaincal`) block. Other rounds use CASA `gaincal`, and the CASA container is only required if some round does. Options merge in the usual order: the options class, then `defaults`, then the round. They are DP3's own settings in DP3's units, so `solint` is in timeslots (ASKAP's are about 10 s) and `smoothnessconstraint` is in Hz.

```yaml
selfcal:
  1:
    dp3ddecal: {mode: scalarphase, solint: 6, nchan: 0}
  2:
    dp3ddecal: {mode: scalarphase, solint: 3, nchan: 1, smoothnessconstraint: 5.0e7}
  3:
    dp3ddecal: {mode: diagonal, solint: 6, nchan: 1, datause: dual, smoothnessconstraint: 5.0e7}
```

Each round writes its solutions to `<ms>.roundN.h5` and applies them to `CORRECTED_DATA`. Amplitude solutions are scaled to a mean of one, as `gaincal`'s `solnorm` does, unless the `dp3selfcal` mode sets `normalise_amplitudes: false`.

| CASA `gaincal` | DP3 `dp3ddecal` |
| --- | --- |
| `calmode: p` | `mode: scalarphase` |
| `calmode: ap` | `mode: diagonal` |
| `solint: 60s` | `solint: 6` |
| `nspw: 4` | `nchan: 1` with `smoothnessconstraint` |
| `uvrange: ">200m"` | `uvmmin: 200` |
| `solnorm: true` | `dp3selfcal: {normalise_amplitudes: true}` |

## Accessing via the CLI

```{argparse}
:ref: flint.dp3.get_parser
:prog: flint_dp3
```
