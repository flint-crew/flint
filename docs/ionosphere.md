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

### Mirroring a whole archive

Instead of fetching maps per observation, you can mirror an IONEX archive once, keep it up to date, and point every run at it.

Flint looks for each map by file name directly inside the directory, as `<ionex directory>/<map file name>`. So the mirror has to be flat: all the map files sit in the one directory, without the server's year and day subdirectories. Leave the files compressed, as they are on the server. Spinifex unpacks them itself.

For the default `chapman` `uqr` maps, on a node with internet:

```bash
IONEX=/path/to/ionex
for year in $(seq 2019 2026); do
    wget --recursive --no-parent --no-directories --timestamping \
         --accept 'uqrg*.Z' \
         --directory-prefix "$IONEX" \
         "http://chapman.upc.es/tomion/rapid/${year}/"
done
```

- `--no-directories` gives the flat layout.
- `--accept` keeps only the map files. wget fetches the directory listings to follow the links, then deletes them.
- `--timestamping` downloads only files that are new or have changed, so running the same command again, e.g. from a nightly cron job, keeps the mirror current.
- Choose the years to cover your observations.

Spinifex reads the day after an observation as well as its own day, to smooth over midnight. So the mirror needs to reach at least one day past the newest observation you correct. New maps appear on the server some time after the day they cover.

Then point flint at the directory, as below. `flint_ionosphere download` is not needed: a missing map still fails straight away, and the error names the command to fetch it.

The pipeline uses the `chapman` `uqr` maps. A mirror of another server or analysis centre can be used with `flint_ionosphere correct --server ... --prefix ...`. The file names must match the server's, e.g. `COD0OPSFIN_*_GIM.INX.gz` or `codg*.Z` for `--server cddis --prefix cod`.

## In the polarisation pipeline

Pass the directory to the polarisation pipeline (or the RACS-All pipeline) with `--ionex-directory`. Every measurement set is then corrected before it is imaged. Without it, nothing is corrected.

## Accessing via the CLI

```{argparse}
:ref: flint.ionosphere.get_parser
:prog: flint_ionosphere
```
