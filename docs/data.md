# Data

## Reference Catalogues

Throughout `flint` there are moments where externally defined reference source catalouges may be used. The expectation is that these are downloaded into a reference directory before workflows or CLI entrypoints are invoked.

To assist `flint_catalogue download` may be used to download the expected set of reference catalogues into a user specified location.

```{argparse}
:ref: flint.catalogue.get_parser
:prog: flint_catalogues
```

The `flint_catalogues download` task will download a set of reference catalogues from [ViZieR](https://vizier.cds.unistra.fr/). The complete list of catalogues may be listed with `flint_catalogues list`. At present there is no mechanism to introduce additional reference catalogues in configuration file based way - they have to be described in source code with an additional `flint.catalgue.Catalogue` class definition.

See `flint.catalogue.KNOWN_REFERENCE_CATALOGUES` (in the source or through programmatically) should a new `ViZieR` catalogue need to be added.

## Sky-model catalogues

The `flint_skymodel` command will attempt to create an in-field sky-model for a
particular measurement set using existing source catalogues and an idealised
primary beam response. Supported catalogues are those available through
`flint_catalogue download`. Note this mode has not be thoroughly tested and may
not be out-of-date relative to how the `flint_flow_continuum_pipeline` operates.
In the near future this may be expanded.

If calibrating a bandpass (i.e. `1934-638`) `flint` will use the packaged source
model. At the moment this is only provided for `calibrate`.

## About ASKAP Measurement Sets

Some of the innovative components of ASKAP and the `yandasoft` package have
resulted in measurement sets that are not immediately inline with external
tools. Measurement sets should first be processed with
[fixms](https://github.com/AlecThomson/FixMS). Be careful -- most (all) `flint`
tasks don't currently do this automatically. Be aware, me hearty.
