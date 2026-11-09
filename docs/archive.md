# Archiving

The last stage of the RACS-all flow archives each field. Where the files go,
and which files, is set in the strategy file under
`defaults.archive.destinations` (see [Configuration](config.md#archive)). If no
destinations are set, nothing is archived.

Each destination has a method and a location:

| Method | Location | What happens |
| --- | --- | --- |
| `copy` | an absolute folder path, or an rclone remote and bucket such as `acacia:racs-all` | `rclone copy` of the matching files to `<location>/SB<sbid>/`, keeping their paths under the field folder. rclone checks the hash of each file after copying, and will not overwrite a file that is already there and differs |
| `tar` | an absolute folder path | one `<location>/SB<sbid>.tar` of the matching files, with its MD5 in `SB<sbid>.tar.md5` next to it. If the tarball is already there it is left alone |

Files are chosen by regular expressions. Each is matched against the whole of
a file's path relative to the field folder, so `rmsynth/.*` takes everything
under the rm-synthesis folder and `.*\.ms\.tar` takes the measurement sets
`--zip-ms` zipped. A zarr store is treated as the files inside it. File names
are passed to rclone as they are, so a name with `*` or `[` in it is not read
as a pattern.

If a pattern matches nothing, a warning is logged. If a whole destination
matches nothing, the stage fails, so a typo in the strategy cannot quietly leave
a product behind.

A destination can also have a `suffix`, which is added to the field name. With
`suffix: .ms` the tarball is `SB<sbid>.ms.tar` instead of `SB<sbid>.tar`, and a
copy goes to `SB<sbid>.ms/`. This is how two destinations can share a folder.
Two destinations that would write to the same place are refused before the run
starts.

## Running it

The stage runs on its own cluster, given by `--archive-cluster-config`. On
Petrichor that should be a data-mover partition. A `copy` destination needs
`--rclone-container` (`flint_containers download` fetches it), and a remote
also needs `--rclone-config`. Put credentials in that rclone config file, and
nowhere else: not in options, strategy files or job scripts. Make it readable
by you alone.

Before the run starts, the stage checks that it will be able to write to every
local location, and that the container and config it needs exist.

Each destination runs as its own task. If one fails the others still run, and
the stage fails afterwards with a message naming each destination that failed
and why.

To retry a field, or to send one archived earlier to a destination added since,
run the stage by itself:

```bash
flint_flow_archive --field-path 60001 --imaging-strategy strategy.yaml \
    --rclone-container rclone.sif --rclone-config ~/.config/rclone/rclone.conf \
    --cluster-config datamover.yaml
```

The flags are the same ones the RACS-all flow takes for its archive stage.

The strategy file only needs `version: 0.2` and the `defaults.archive`
section. Nothing else in it is read.

Running the stage again on an archived field is safe. Files already at a `copy`
destination are compared and not copied again, and a finished tarball is kept.
