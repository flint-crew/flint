# Source Finding

Current in `flint` [the `aegeantools` package](https://github.com/PaulHancock/Aegean) is the only source finder supported. Even so, it is only used through a container as a CLI callable and not directly accessed via its private API.

There are two reasons where `aegeantools` is currently used:

1 - background and noise maps
2 - slim source finding

## Background and noise estimation

There are weak links to the `BANE` program packaged in `aegeantools`. The computation of the `bkg` and `rms` maps through a rolling sigma-clipping boxcar function produces reliable and easily accessible direction-dependent diagnostics. In early versions of `flint` these were used as the basis of our clean mask creation routines. The `rms` map is also used when creating a summary field figure.

We note that in the latest version of `aegeantools` that there is [an ongoing bug that can lead to a deadlock issue](https://github.com/PaulHancock/Aegean/issues/198). For these reason we only expose the `cores` argument and have logic to ensure that the `stripes` `BANE` parameters it always one less than this.

```{literalinclude}  ../flint/source_finding/aegean.py
:pyobject: BANEOptions
```

## Source finding

`Flint` offers the `aegean` source finding tool to be executed after `BANE` has been invoked. These two tools will always run sequentially. A limited set of `aegean` options have been exposed.

Importantly the intention here is to obtain a set of diagnostic results with minimal computation. The `AegeanOptions` exposed correspond to equivalent `aegean` CLI arguments. Do not that the default value of `noconv` is `True` -- this mode does not handle the correlation of adjacent pixels.


```{literalinclude}  ../flint/source_finding/aegean.py
:pyobject: AegeanOptions
```

```{admonition} Caution
:class: caution

The `flint_aegean` CLI will append options from both `BANEOptions` and `AegeanOptions`, and craft instances of these objects automatically. The default type of `nocon` and `autoload` will be translated to `store_false` options in `argparse.ArgumentParser`. If you want to use the quick version of `aegean` do not pass `--noconv` to the `flint_aegean find` CLI call.
```

## Accessing via the CLI

The primary entry point for the source finding with `aegean` in  `flint` is via `flint_aegean`:

```{argparse}
:ref: flint.source_finding.aegean.get_parser
:prog: flint_aegean
```
