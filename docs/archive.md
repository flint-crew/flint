# Archiving

The RACS-all flow ends with an archive stage, which sends each field's folder to
the destinations the strategy file defines under `defaults.archive.destinations`
(see [Configuration](config.md#archive)). Each destination says how it is
reached and which files it takes:

| Method | Location | What it does |
| --- | --- | --- |
| `copy` | a folder, or an rclone remote and bucket such as `acacia:racs-all` | `rclone copy` of the matching files to `<location>/SB<sbid>/`, keeping their paths. rclone checks the hash of every copy, and refuses to overwrite a file that differs |
| `tar` | a folder | one `<location>/SB<sbid>.tar` of the matching files, with its MD5 in `SB<sbid>.tar.md5` beside it. A tarball already there is kept |

Files are picked by regular expressions matched whole against their path
relative to the field folder, so `rmsynth/.*` takes the rm-synthesis products
and `.*\.ms\.tar` the measurement sets `--zip-ms` zipped. A zarr store is just
its files. With no destination defined, nothing is archived.

## Running it

The stage runs on its own cluster, from `--archive-cluster-config`, which should
sit on a data-mover partition. Every `copy` destination needs
`--rclone-container` (`flint_containers download` fetches it), and a remote
needs `--rclone-config`. Keep credentials in that rclone config file, readable
by you alone, rather than in options, strategy files or job scripts.

Each destination runs as its own task. One that fails does not stop the others,
but fails the run once all have finished. To retry, or to send a field archived
earlier to a destination added since, run the stage on its own:

```bash
flint_flow_archive 60001 --strategy-yaml-path strategy.yaml \
    --rclone-container rclone.sif --rclone-config ~/.config/rclone/rclone.conf \
    --cluster-config datamover.yaml
```

Files already at a destination are compared, not copied again, and a finished
tarball is left as it is.
