(config)=
# Configuration

## CLI Configuration file

To help manage (and avoid) long CLI calls to configure `flint`, most command
line options may be dumped into a new-line delimited text file which can then be
set as the `--cli-config` option of some workflows. See the `configargparse`
python utility to read up on more on how options may be overridden if specified
in both the text file and CLI call.

## Strategy file

To help wrangling the many options available in `flint` we use a 'strategy' file. These options are those that would
be used by stages throughout a workflow, and are used to help track how options may evolve across operations that
may be repeated (e.g. imaging and self-calibration).

This file is written in YAML and has the following rough layout:

```yaml
defaults:
    mode:
        option1: X
        option2: Y

operation1:
    mode:
        option1: Z

operation2:
    mode:
        option2: A
```

The `defaults` section sets the 'global' default for a given option, which can be updated in given context e.g. a particular round of self-calibration.

An 'operation' refers to flow context in which a particular tool is being usef. As of version 0.2, the following 'operations' are supported:

- `selfcal`
- `stokesv`
- `subtractcube`
- `polarisation`
- `rmsynth`

A 'mode' refers to a set of options for a given tool. As of version 0.2, the following 'modes' are supported:

- `wsclean` : This corresponds to `flint.imager.wsclean.WSCleanOptions`
- `gaincal` : This corresponds to `flint.selfcal.casa.GainCalOptions`
- `masking` : This corresponds to `flint.masking.MaskingOptions`
- `archive` : This corresponds to `flint.options.ArchiveOptions`
- `bane` : This corresponds to `flint.source_finding.aegean.BANEOptions`
- `aegean` : This corresponds to `flint.source_finding.aegean.AegeanOptions`
- `potatopeel`: This corresponds to `flint.peel.potato.PotatoPeelOptions`
- `fitscube`: This correspond to `flint.options.FitsCubeOptions`
- `tukeytractor`: This corresponds to `flint.options.TukeyTractorOptions`
- `concatholo`: This corresponds to `flint.options.ConcatHoloOptions`
- `rmsynth`: This corresponds to `flint.options.RMSynthOptions`
- `rmclean`: This corresponds to `flint.options.RMCleanOptions`
- `spice`: This corresponds to `flint.options.SpiceOptions`

To see all the available options you can run `flint_{mode} -h` on the command-line.
All attributes available in the corresponding `Options` class listed above may be
provided in the strategy YAML file.

As a general rule, the following hierarchy is used to set a value for a given option:

- The value given in a CLI call
- The specific context in a given 'mode' (e.g. round of self-cal)
- The value in `defaults`
- The default value in the `Options` class
- The default value in a calling function
- The default value in the external tool

Note that not all options are available in all of the above locations.

### `fitscube`

In the self-cal flows (`continuum_pipeline`, `racs_all_continuum_selfcal`),
`fitscube` options are looked up per round like `wsclean`/`gaincal`/
`masking`: `selfcal.<round>.fitscube` in the strategy file overrides
`defaults.fitscube` for that round — with one deliberate exception,
`compress` (see below).

`subtract_cube_pipeline` and `polarisation_pipeline` are not self-cal pipelines so
a single value from `defaults` (or the `subtractcube`/`polarisation` operation block) is
used throughout.

#### `compress`

`FitsCubeOptions.compress` gzip-compresses a finished cube. Compression is
only safe for a cube that is never reopened later in the pipeline: `astropy`
cannot memmap a gzip file, so reading one back decompresses the whole cube
into memory, and `split_cube_into_planes` refuses to run on a compressed cube
for exactly this reason (see `flint.imager.wsclean.split_cube_into_planes`).

In the self-cal flows, each round reassigns the `wsclean_results` variable to
that round's own output, so only the **final** round's per-beam cube survives
the loop and feeds `create_convolve_linmos_cubes`, which splits it into
planes to co-add across beams. Earlier rounds' per-beam cubes are discarded
once the next round starts and are never split. So `compress` cannot simply
follow the usual per-round lookup: `get_selfcal_round_fitscube_options`
(`flint.configuration`) only forces `compress=False` for the final round's
per-beam cube; every other round's `fitscube` options, including
`compress`, are drawn from the strategy file exactly as written, with no
overriding. The separate, co-added cube produced by
`create_convolve_linmos_cubes` at the end of the loop is unaffected by this
and honours `compress: true` set under
`defaults.fitscube` (or a `selfcal.<round>.fitscube` override for the final
round) as normal. If you set `compress: true` for the final round's per-beam
cube, it is ignored and a warning is logged.

### `rmsynth` and `rmclean`

`flint_flow_rmsynth_pipeline` (and the `rmsynth` stage of the racs-all flow)
draws its algorithm parameters from the `rmsynth` operation block, which holds
two modes: `rmsynth` (`flint.options.RMSynthOptions`) and `rmclean`
(`flint.options.RMCleanOptions`). Everything else about the stage -- which
cubes to read, which products to write, where they go -- comes from
`RMSynthFieldOptions` on the command line instead.

```yaml
rmsynth:
  rmsynth:
    phi_max_radm2: 1000.0
    n_samples: 10.0
    weight_type: variance
    estimate_stokes_i_noise: true
    stokes_i_snr_cut: 5.0
    fit_order: 2
    moment_threshold_snr: 5.0
    target_chunk_mb: 256
    nufft_nthreads: 1
  rmclean:
    auto_mask: 7
    auto_threshold: 1
    gain: 0.1
    max_iter: 100000
```

#### The Stokes I fit dominates the runtime unless it is given a noise

Passing `--stokes-i-cube` turns on rm-lite's per-pixel fractional-polarisation
correction: every pixel's Stokes I spectrum is fitted, Q/U are divided by that
model, and the FDF is rescaled to flux at the reference frequency.
`stokes_i_snr_cut` is what keeps that affordable -- below it a pixel takes a
flat model (no correction) rather than a fit.

The cut needs something to measure SNR against. rm-lite computes
`mean(I) * sqrt(n) / rms(error)` and deliberately returns infinity when the
error spectrum is all-zero, so that an SNR cut degrades to a no-op rather than
rejecting everything. So with no Stokes I error at all, **every** pixel scores
infinity, passes the cut, and gets a full bounded `curve_fit`. On the
800-1799 MHz RACS-all grid that is ~25 ms per noise pixel against ~17 us for a
pixel the cut rejects, and noise pixels are almost the whole cube.

It is not only slow. A second-order power law fitted to a pure-noise spectrum
is unconstrained, and half of such fits reach a minimum of `~1e-10` somewhere
in the band. Dividing Q/U by that gives an infinite FDF, an infinite `mom0`,
and an RM-CLEAN that grinds to `max_iter` on the pixel.

So give it a noise, one of:

- `estimate_stokes_i_noise: true` (the default): a robust per-channel MAD from
  the Stokes I cube itself. One extra frequency-chunked pass over the cube.
- `--stokes-i-error-cube`: a matching error cube, giving a per-pixel noise.
  Takes precedence over the estimate above.

`flint.rmsynth.run_rmsynth_3d` warns when neither is in play and the cut is
therefore inert.

#### Faraday depth range sets the cost of everything downstream

`phi_max_radm2` and `n_samples` (or `d_phi_radm2`) fix the length of the
Faraday depth axis, and both RM-synthesis and RM-CLEAN cost scale with it --
as does the per-chunk memory, since rm-lite shrinks spatial chunks to keep a
complex128 FDF chunk inside the budget `target_chunk_mb` implies.

Left as `null`, `phi_max_radm2` is derived as `sqrt(3) / max(diff(lambda^2))`,
floored at ten RMSF FWHM. That derivation reads the frequency axis in the
header, so it depends on how the cube was built rather than on what you want:
for RACS-all low+mid+high stitched onto one linear 8 MHz grid from 800 to
1799 MHz (125 channels, of which the 34 in the two coverage gaps are blank),
the largest lambda^2 step is the 8 MHz step at the bottom of the band, not the
gaps, and the derived range comes out at +/-636 rad/m^2 with 373 Faraday
depths. Drop the blank gap channels and the same derivation lands on
+/-342 rad/m^2 instead. Set it explicitly rather than inheriting either.

For reference, on that grid the RMSF FWHM is 34.2 rad/m^2, so `n_samples: 10`
gives `d_phi = 3.42 rad/m^2`, and `phi_max_radm2: 1000` gives 587 Faraday
depths (1.6x the derived default's cost and memory).

#### Faraday moment maps

flint builds its own mom0/mom1/mom2 maps from whichever FDFs
`--moment-products` asks for, rather than using the ones RM-CLEAN computes
internally, so that the FDF cube they reduce never has to be gathered into one
worker. `RMSynthOptions.moment_threshold_snr` is the cut applied to FDF
amplitudes first, and it applies to the dirty FDF as much as the cleaned one:
`mom0` sums `|FDF|` over every Faraday depth, so with no cut an off-source
pixel integrates hundreds of noise samples into a large positive floor and
`mom1`/`mom2` are then weighted by that noise.

`RMCleanOptions.moment_threshold_snr` is a different knob with the same name:
it only reaches rm-lite's internal maps, which flint does not write.

#### Multiscale RM-CLEAN

`multiscale: true` recovers Faraday-thick structure, and is exposed along with
its scale/kernel parameters (`multiscale_scales`, `multiscale_n_scales`,
`multiscale_kernel`, ...). It is experimental in rm-lite and much slower than
single-scale -- it fits a Gaussian to every scale kernel of every pixel it
cleans -- and in rm-lite 2026.8.1 it raises on a fully blanked (all-NaN)
spectrum, which a mosaic edge has many of. Leave it off for survey-scale runs.

#### Cubes must not be compressed

RM-synthesis reads each cube block by block, and `astropy` cannot memmap a
gzip file, so every block read of a compressed cube would decompress the whole
thing. `flint.rmsynth` raises `NotSupportedError` on a `.gz` input, and the
racs-all flow defers compressing the polarisation cubes until after the
rm-synth and spice stages have read them.

## Configuration based settings in Python API

Most settings within `flint` are stored in immutable option classes, e.g.
`WSCleanOptions`, `GainCalOptions`. Once such an option class has been
created, any new option values may only be set by creating a new instance. In
such cases there is an appropriate `.with_options` method that might be of use.
This 'nothing changes unless explicitly done so' was adopted early as a way to
avoid confusion when moving to a distributed multi-node execution environment.

The added benefit is that it has defined very clear interfaces into key stages
throughout `flint`s calibration and imaging stages. The `flint_config` program
can be used to create template `yaml` file that lists default values of these
option classes that are expected to be user-tweakable, and provides the ability
to change values of options throughout initial imaging and subsequent rounds of
self-calibration.

In a nutshell, the _currently_ supported option classes that may be tweaked
through this template method are:

- `WSCleanOptions` (shorthand `wsclean`)
- `GainCalOptions` (shorthand `gaincal`)
- `MaskingOptions` (shorthand `masking`)
- `ArchiveOptions` (shorthand `archive`)
- `BANEOptions` (shorthand `bane`)
- `AegeanOptions` (shorthand `aegean`)
- `FitsCubeOptions` (shorthand `fitscube`)

All attributes supported by these options may be set in this template format.
Not that these options would have to be retrieved within a particular flow and
passed to the appropriate functions - they are not (currently) automatically
accessed.

The `defaults` scope sets all of the default values of these classes. The
`initial` scope overrides the default imaging `wsclean` options to be used with
the first round of imaging _before self-calibration_.

The `selfcal` scope contains a key-value mapping, where an `integer` key relates
the options to that specific round of masking, imaging and calibration options
for that round of self-calibration. Again, options set here override the
corresponding options defined in the `defaults` scope.

`flint_config` can be used to generate a template file, which can then be
tweaked. The template file uses YAML to define scope and settings. So, use the
YAML standard when modifying this file. There are primitive verification
functions to ensure the modified template file is correctly form.

## `BaseOptions` class

All (or most) of `flint`'s `Options` classes are derived from `flint.options.BaseOptions`. This uses `pydantic` in order to validate upon class initialisation that the provided values match the types they are listed as having. For instance, if a `float` of a value `3.141` was provided to an option with type `int` it will be converted appropriately. This is also true for values described in a strategy file.

If the values are unable to be coerced into a correct type than `pydantic` will raise an error.

All `flint` `Option` classes should be derived from `BaseOptions`:

```python
from flint.options import BaseOptions


class PirateOptions(BaseOptions):
    """An Options class used to create a new Pirate"""

    name: str
    """Name of the pirate we will create"""
    age: int = 34
    """The age, in years, of the pirate"""
    weaknesses: list[str] | None = None
    """Should the pirate have any weaknessess they go here"""
```

Should `PirateOptions(name="Jack Sparrow", age=34.23, weaknesses=None)` be invoked, the `age=34.23` input, which is a `float`, will be cast to an `int`.
