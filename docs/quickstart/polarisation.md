# Polarisation imaging

`flint` supports full-Stokes imaging, with a particular emphasis on supporting polarisation cubes. The main entry point to polarisation imaging is `flint_flow_polarisation_pipeline`. This flow assumes that the input visibilities are already calibrated to the user's satisfaction, and simply provides an interface to imaging in Stokes I, Q, U and V. Whilst WSClean typically doesn't produce 'cubes' in the same fashion as ASKAPsoft or CASA (see notes [here](https://wsclean.readthedocs.io/en/latest/making_image_cubes.html)), `flint` utilises [`fitscube`](https://github.com/AlecThomson/fitscube) to combine the per-channel images into a single FITS file.

Currently, we require the filenames of the input data to follow either the `flint` or CASDA naming scheme. In the latter case, appropriate transformations will be applied to the data to allow the use of WSClean.

## Configuration

The same imaging strategy file can be used for both polarisation imaging and {ref}`self-calibration <strategy>`. Similar to `selfcal`, the section for polarisation imaging is called `polarisation`, with the following three subsections:

- `total`: Options for Stokes I. Will always set `-pol I` in the WSClean command.
- `linear`: Options for Stokes Q and U. Will always set `-pol QU` in the WSClean command.
- `circular` Options for Stokes V. Will always set `-pol V` in the WSClean command.

Currently, only the `wsclean` options will be used by the `polarisation` section. The values from `defaults` will propagate, as expected. For example, assuming the required values have been set in `defaults` a polarisation section could look like:

```yaml
polarisation:
  total:
    wsclean:
      squared_channel_joining: false
      no_mf_weighting: true
  linear:
    wsclean:
      join_polarizations: true
      squared_channel_joining: true
      no_mf_weighting: true
      multiscale: false
      local_rms: false
  circular:
    wsclean: {}
```

## Angular resolution

The restoring beam of an ASKAP image varies across the footprint and with
frequency, so the per-beam channel images have to be brought to a common
resolution before `linmos` co-adds them. `flint` does this in the 'natural'
sense of [`RACS-tools`](https://github.com/AlecThomson/RACS-tools): one common
beam is solved **per channel** and the cube keeps its frequency-dependent
resolution, rather than every channel being dragged out to the coarsest beam in
the band.

A channel's beam is solved over every footprint beam of every Stokes at that
channel, so channel N of I, Q and U share one resolution and a per-channel
polarisation product (e.g. polarised intensity) is meaningful.

Two options steer this:

- `--beam-cutoff`: a beam coarser than this, in arcseconds, is left out of the
  solve and its image is blanked instead of convolved. A channel where every
  image is beyond the cutoff has no beam to convolve to at all, so the whole
  channel is blanked and marked as carrying no PSF.
- `--fixed-beam-shape`: convolve every channel to this `(arcsec, arcsec, deg)`
  beam instead of solving one, which gives a cube of constant resolution.

RM-synthesis is only meaningful when every channel shares one beam, so the
rm-synth stage brings its own inputs to a single 'total' beam covering the whole
band. It writes those as new `.conv.fits` cubes and leaves the natural-resolution
cubes alone, so both are available: the natural cubes to archive, the total cubes
to synthesise from. See `RMSynthFieldOptions.beam_cutoff` to drop the coarsest
channels from that solve rather than smoothing the whole band to reach them.

## Spectro-polarimetric imaging in WSClean

We encourage users to carefully read the [WSclean documentation](https://wsclean.readthedocs.io/en/latest/). In practice, we have encountered a few common 'gotchas' when producing polarisation cube. As always, a user should pay attention to the output logs to see e.g. how many iterations have been performed and what the stopping criterion was. We also encourage the inspection of image, model, and residual products to see how well (or not) deconvolution has performed.

As of writing, these generally apply to `wsclean <= 3.5` and may change with future releases:

- Using `-squared-channel-joining` with `-multiscale` is known to [get stuck](https://wsclean.readthedocs.io/en/latest/rm_synthesis.html#using-entire-bandwidth-for-cleaning-qu-cubes).
- Enabling `-squared-channel-joining` appears to change how image statistics should be interpreted. We have not confirmed this directly, but in seems in practice one should square the values for `-auto-mask` and `-auto-threshold` when squared channels are enabled. For example, if you wanted `-auto-mask 5` in Stokes I, you should set `-auto-mask 25` for Stokes Q and U when using `-squared-channel-joining`. There also appears to be a [bug](https://gitlab.com/aroffringa/wsclean/-/issues/177) when `-local_rms` is also enabled.
- The WSclean docs [recommend](https://wsclean.readthedocs.io/en/latest/mf_weighting.html) turning multi-frequency weighting _**off**_ when using the individual channel images for science.

## Accessing via the CLI

```{argparse}
:ref: flint.prefect.flows.polarisation_pipeline.get_parser
:prog: flint_flow_polarisation_pipeline
```

## The `PolFieldOptions` class

Embedded below is the `flint` `Options` class used to drive the `flint_flow_polarisation_pipeline` workflow. Input values are validated by `pydantic` to ensure they are appropriately typed.

```{literalinclude}  ../../flint/options.py
:pyobject: PolFieldOptions
```
