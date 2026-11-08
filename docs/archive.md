# Archiving

The RACS-all flow ends with an archive stage, which copies each field's final
products to opt-in destinations and verifies every copy, so that the processing
folder can be purged safely afterwards.

| Destination | Switched on by | Takes | How |
| --- | --- | --- | --- |
| Hot copy | `--sbid-copy-path` | `image`, `catalogue`, `report` | rclone, folder to folder |
| Tarball | `--sbid-archive-path` | every kind | one tar per field, with a `.md5` beside it |
| Object stores | `--archive-remotes` | the kinds each remote lists | rclone, one remote at a time |

Remotes are defined once in the strategy file, under `defaults.archive.remotes`
(see [Configuration](config.md)), and a run names the ones it uses. With no
destination set, nothing is archived.

## The manifest

Each run writes `archive_manifest.json` into its processing folder. It lists every
product, by path relative to that folder and by kind, and every destination the
field must reach. Products come from what the stages hand over, plus files matched
by `defaults.archive.sweep_patterns`. The archive stage first records each
product's size and MD5, then copies, and writes the manifest to each destination
last as its marker. Set `--zip-ms` to zip the final-round measurement sets into
the tarball.

## Running it

The stage runs on its own cluster, from `--archive-cluster-config`, which should
sit on a data-mover partition. The hot copy and remotes also need
`--rclone-container` (`flint_containers download` fetches it), and remotes need
`--rclone-config`. Keep credentials in that rclone config file, readable by you
alone, rather than in options, strategy files or job scripts.

To retry a failed archive, or send a field archived earlier to another
destination, run the stage again from the manifest:

```bash
flint_flow_archive run 60001/archive_manifest.json \
    --archive-remotes dap --strategy-yaml-path strategy.yaml \
    --rclone-container rclone.sif --rclone-config ~/.config/rclone/rclone.conf
```

Destinations whose marker already matches are skipped. Before purging a
processing folder, check that every destination holds the field:

```bash
flint_flow_archive check 60001/archive_manifest.json \
    --rclone-container rclone.sif --rclone-config ~/.config/rclone/rclone.conf
```

It exits with 0 only when every destination does.

## Limiting concurrent copies

Each destination task is tagged `flint-archive`. To cap how many run at once
across all fields, create a tag concurrency limit on the Prefect server the runs
share:

```bash
prefect concurrency-limit create flint-archive 4
```

The limit only spans runs that report to the same Prefect server. A large cube
sent to the tarball and two remotes is read about six times, so the limit is also
what keeps the archive from crowding the processing filesystem.
