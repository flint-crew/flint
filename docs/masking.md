# Creating Clean Masks

The imaging performance of CLEAN and its derivatives can be improved through the construction of reliable clean mask regions.  These regions restrict the set of pixels that are involved during the peak finding stage of CLEAN. Here the aim of the game is to optimally select pixels that contain genuine emission. Issues such as clean bias. sub-optimal self-calibration and clean divergence can be minimised or outright eliminated provided a clean mask has been reliable constructed.

`Flint` provides functionality to construct such clean masks, where the output is a pixel-wise mask of arbitrary shapes that either allow of deny cleaning to occur at certain pixel locations. These are intended to be evaluated against a restored FITS image. Pixels in the output clean mask are either `0` (cleaning is not allowed here) or `1` (cleaning is allowed here).

## Available statistics

`Flint` currently supports two statistics to identify pixels of significance that should be cleaned.

### Signal-to-noise (SNR)

The obvious method is based on signal-to-noise (SNR). After constructing  background ({math}`\mu`) and noise ({math}`\sigma`) measures across an image a direction-dependent SNR ({math}`\mathrm{SNR}`) across the image ({math}`\mathrm{img}`) can be expressed:

{math}`\mathrm{SNR}(x,y) = \frac{\left(\mathrm{img}-\mu\right)}{\sigma}.`

In the simplest case constant values may be set across the extent of the image {math}`\mu=0`, and {math}`\sigma=\mathrm{SD}(\mathrm{img})`. These can be replaced with more sophisticated schemes that compute position dependent metrics that also incorporate iterative outlier clipping (e.g. [Background and Noise Estimator](https://github.com/PaulHancock/Aegean)) or robust statistics (e.g. [Selavy](https://www.atnf.csiro.au/computing/software/askapsoft/sdp/docs/current/analysis/selavy.html)).

At their heart these SNR based processes are assuming Gaussian distributed zero-mean noise, where pixel intensities that are unlikely to occur by chance are assumed to be genuine emission. That is to say, pixels that are {math}`\gg5\sigma` are likely to be real and should be included in a mask for cleaning.

### Minimum absolute clip

SNR based measures have a clear statistical foundation when identifying bright pixels. However, there are situations when such a metric could be perturbed or otherwise corrupted, including:

- Calibration errors that produce imaging artefacts,
- Deconvolution errors that accumulate over minor/major iterations,
- Misshandling of the w-term so that the 2D Fourier transform approximation breaks down,
- Extended diffuse structures that represents a large fraction of the region used to calculate the local {math}`\mu` or {math}`\sigma` quantities,
- Combinations of the above, or
- Gremlins lurking around in the data.

Ultimately, if the regions being considered in the derivation of the noise do not appear Gaussian like, the robustness of methods to calculate the local noise level are suspect. Additionally, it is unclear whether an accurate noise estimation is even an appropriate basis for such regions. For instance, artefacts around a bright sources produced by phase error could be {math}`\gg\sigma` if the genuine source is sufficiently bright.

We introduce the Minimum Absolute Clip ({math}`\mathrm{MAC}`) as an alternative metric. By assuming:

1. approximately zero-mean distributed Gaussian noise,
2. the sky brightness is positive definite, and
3. should there be a significantly bright negative component there will be a positive one of comparable brightness nearby.

These assumptions are fair for Stokes-I images. A masking function can therefore be constructed, where:

{math}`\mathrm{MAC}(x, y) = \mathrm{img} > P \times |\mathrm{RollingMinimum}\left(\mathrm{img}, \mathrm{BoxSize}(\mathrm{img}, x, y)\right)|`

where {math}`\mathrm{RollingMinimum}` is a minimum boxcar filter of dimensions return by {math}`\mathrm{BoxSize}`pixels, which could be turned in a position dependent way. The absolute value of the {math}`\mathrm{RollingMinimum}` function is increased by a padding factor {math}`P` to increase the minimum positive threshold. The {math}`\mathrm{MAC}` is a simply and efficient statistic to compute.

The choice of an appropriate boxcar size is an important consideration. A larger size enforces more conservative behaviour from the {math}`\mathrm{MAC}` process. Should too small a region be supplied than typically large multi-scale features are less reliably assessed. Extensions can be made to detect when an the rolling box filter size is too small (e.g. by an imbalance of positive to negative pixels). A basic implementation of this adaptive boxcar size is implemented via the `MaskingOptions.flood_fill_use_mbd_adaptive` parameters.

<!-- TODO: Need to include some image here -->

## Reverse flood filling

Cleaning source with features that are both faint and diffuse is difficult. On a per-pixel basis it is often a reality that a minimum signal-to-noise threshold is not met. Should the cleaning thresholds (set in either absolute {math}`\mathrm{Jy}/\mathrm{beam}` units or in terms of {math}`\sigma` units) be too high this diffuse emission is never cleaned. In `flint` we include a reverse flood fill procedure that grows islands of pixels above an initial criteria out to adjacent pixels that met a secondary lower level. This process is often the initial step of most source finders.

There are two steps to this process:

1. _Initial siginficant island detection_: The statistic of choice (see above) is executed and all pixels above the threshold are marketed as pixels to clean. We can set this via `MaskingOptions.flood_fill_positive_seed_clip`
2. _Growing the initial set of islands_; Islands that are identified as significant go through a dilation process, where the dilation process is restricted to limit it only to pixels above the lower level threshold. This lower level clip is set via `MaskingOptions.flood_fill_positive_flood_clip`.

Activating the flood fill procedure is 'opt in' and activated via `MaskingOptions.flood_fill`. Further, the {math}`\mathrm{MAC}` statistic is only exposed when the flood fill is enabled.

<!-- TODO: Need to include some other image here -->

## Eroding output islands

Output binary clean masks are naturally at the resolution of the input image (at least in `flint`). All pixels that reside in an island are candidate pixels where cleaning is allowed to occur. Consider an unresolved source that is perfectly aligned to the discrete pixel grid. In principal its clean component should be contained entirely to a single pixel. However, as the source is sufficiently bright the resulting mask is made up of many pixels (i.e. the shape the restoring beam). Should deep cleaning be employed it is possible that small perturbations from the noise underlying a source would also be cleaned.

A binary erosion process with the erosision structure set to the shape of the restoring beam at a particular power level can be used to 'contract' all pixel masks. The output should better reflect where clean components ought to be placed. See the `MaskingOptions.beam_shape_erode` and `MaskingOptions.beam_shape_erode_minimum_response` options to activate and control this process. The minimum response should be set between 0 to 1, where numbers closer to 0 represent a larger binary erosion structure shape. That is to say islands need to be _larger_ for any pixels to remain as `MaskingOptions.beam_shape_erode_minimum_response` approaches 0.

## Accessing via the CLI

The masking utility may be accessed via a CLI entrypoint:

```{argparse}
:ref: flint.masking.get_parser
:prog: flint_masking
```

## The `MaskingOptions` class

Embedded below is the `flint` `Options` class used to construct clean masks. Input values are validated by `pydantic` to ensure they are appropriately typed.

```{literalinclude}  ../flint/masking.py
:pyobject: MaskingOptions
```
