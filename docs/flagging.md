# Flagging Visibilities

`Flint` currently flags visibilities through two main mechanisms:

1 - Using an `ASKAP.lua` script (see `flint.data.aoflagger`) provided to a containerised version of `aoflagger`
2 - Flagging of known bad systematics

## Flagging with `aoflagger`

Automated RFI flagging is carried out with `aoflagger`. We have packaged with `flint` a customised `lua` script that attempts to craft an appropriate flagging strategy. This `lua` script flags based on smoothness along the frequency axis. Flagging occurs per-baseline on each of the recorded instrumental polarisation products.

The default `ASKAP.lua` strategy adds to the existing set of flags. Any flags that are set as `True` of the input measurement set are retained after processing. Data values that are either not-a-number (NaN) or are zero'd are also flagged.

## Manual flagging

Some stages in `flint` also do an additional round of flagging as a 'just in case'. Data will be forcefully flagged if:

1 - data are recorded as being `0` or `NaN`
2 - the `uvw` is recorded as `(0, 0, 0)`
3 - significant Stokes-V emission

Optionally, any data whose corresponding flag is set to `True` can be `NaN`.

This functionality is implemented in `flint.flagging.nan_zero_extreme_flag_ms` function0n.

## Accessing via the CLI

The flagging utilities via the CLI in `flint` can be done via `flint_flagging`:

```{argparse}
:ref: flint.flagging.get_parser
:prog: flint_flagging
```
