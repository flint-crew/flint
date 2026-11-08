# Ionospheric Faraday rotation

`Flint` can remove the Faraday rotation of the ionosphere from measurement sets with [spinifex](https://spinifex.readthedocs.io/). It is installed with the `iono` extra (which brings in DP3):

```bash
pip install 'askap-flint[iono]'
```

For each measurement set, spinifex models the ionospheric rotation measure towards the phase centre of each antenna over time, and writes it to an h5parm. DP3's `applycal` then removes it, writing the corrected visibilities to `IONO_CORRECTED_DATA`. The measurement set returned tracks that column, so `wsclean` images it.

## Running it twice is safe

The correction always reads the uncorrected column and overwrites `IONO_CORRECTED_DATA`, so running it again redoes the correction rather than applying it twice. A measurement set already tracking `IONO_CORRECTED_DATA` is refused. Each correction is also recorded in the measurement set's `HISTORY` table.

The cost is one extra copy of the visibilities per measurement set.

## Offline worker nodes

Spinifex needs IONEX maps of the ionosphere, which have to be downloaded. Worker nodes often have no internet, so the maps are fetched beforehand, on a node that does (e.g. a login node):

```bash
flint_ionosphere download /path/to/ionex /path/to/SB12345/*.ms
```

This fetches exactly the maps the correction will read, including the following day's map that spinifex uses to smooth over midnight. The directory can be shared between fields.

The correction itself never touches the network. It only reads maps from the directory, failing straight away if one is missing, and astropy uses its bundled Earth orientation tables. The geomagnetic field model ships with spinifex.

By default the maps are the `uqr` maps from the `chapman` server. `--server` and `--prefix` choose others. Maps from `cddis` need NASA Earthdata credentials in `~/.netrc` on the node that downloads them.

## In the polarisation pipeline

Pass the directory to the polarisation pipeline (or the RACS-All pipeline) with `--ionex-directory`. Every measurement set is then corrected before it is imaged. Without it, nothing is corrected.

## Accessing via the CLI

```{argparse}
:ref: flint.ionosphere.get_parser
:prog: flint_ionosphere
```
