# Why cublet imaging works the way it does

This page gives the reasoning behind the [cublet imaging](cublet.md) flow. It
is for anyone who wants to judge the approach, change it, or work out why a run
went wrong. How to run the flow is on the other page.

## The problem

A spectral cube of a whole ASKAP beam does not fit in memory at fine frequency
resolution. An image of 10,000 pixels a side is about 400 MB a channel. At 72
channels that is 30 GB, and while cleaning with joined channels wsclean holds a
residual, a model and a PSF for every channel. The limit on the cube is the
memory of the machine, not the data.

Most of that memory holds empty sky. The emission sits in small islands that
together cover a tiny part of the image.

## The idea

Find the islands on an image that is cheap to make, then image each island on
its own in a window just large enough to hold it. A window of 128 pixels at 72
channels is 5 MB an image. The channel count of an island is then set by what
its window costs, not by what the whole field costs.

The cheap image is the same field with few channels. It is only used to find
the islands, so it does not have to be cleaned deeply.

## Why an island can be imaged alone

Three things have to hold for a small image to stand in for part of a large
one.

**The same pixels.** The wsclean `-shift` option moves the centre of the image
but keeps the projection of the larger image. Shifting to the centre of a pixel
of the large image gives a window whose pixels are pixels of the large image.
The mask of an island is therefore a cutout of the mask of the field, with no
regridding.

**The same weights.** wsclean sizes its weight grid from the image size, so a
small image would get a different weighting, and a different PSF, from the
field. Setting `-super-weight` to the ratio of the two padded sizes gives the
island the weight grid of the field.

**Nothing else in the window.** Another island may fall inside a window. Its
pixels are removed from the mask of the window, so each island is only ever
cleaned by its own call. Without this its flux would be counted twice.

## How islands affect each other

Islands that are imaged apart still share the visibilities. Each one sits in
the sidelobes of all the others. The flow deals with this the way a major cycle
does: the components found for every island are predicted, subtracted from the
data, and the islands are imaged again. In this flow a round is a major cycle,
which is why wsclean is run with an `mgain` of 1 and makes none of its own.

Every island in a round is cleaned to the same level. If one were cleaned far
deeper than the rest, it would be cleaning the sidelobes of its neighbours into
its own model.

## Why the rounds can be deep

In a normal clean the major cycles are kept shallow so that a sidelobe is never
mistaken for a source. Here the mask does that job. The masks come from
eye-patch, and with a high seed clip they hold real emission and little else.
A sidelobe that falls outside the mask of an island can not be cleaned.

A sidelobe that falls inside a mask can still be taken into the flux of that
island. This is an error in flux and not a false source, and it is undone in
the next round, once the neighbour that caused it has been subtracted.

So a round may go down to about the far sidelobe level of the PSF below the
brightest island. The default `round_gain` of 0.97 assumes that level is 3%.
It has not been measured for ASKAP, and is worth checking against a PSF.

Trusted masks also allow a high loop gain with few iterations. That keeps the
number of components small, which matters because every component has to be
predicted.

## Why cycles, and why old islands stay open

The first mask only holds what stands clear of the sidelobes of the bright
sources. Once those are subtracted, the image is cleaner and a new mask
reaches fainter emission. A cycle is this step: image what is left, mask it,
and run the rounds again.

The mask of a cycle is joined with the masks before it. The other choice was to
mask only what is left, which would close every island once it had been
cleaned. That has two faults. An island that was cleaned too far leaves a
negative hole, which a mask of positive emission never covers. And the faint
edge of an island found in an earlier cycle would become an island of its own.
With the joined mask an island is one region for as long as the flow runs. What
a later cycle finds there is a correction to what is already in the model, and
it may be negative.

For the same reason, what is left of an island is measured as an absolute
value.

## The model

The components of an island are saved with `-save-source-list` and predicted
with crystalball. This means the spectrum of each component is a polynomial.
That is enough for total intensity continuum, and it lets the prediction run
without the images.

It also sets the limit of the approach. Anything whose spectrum is not smooth,
linear polarisation above all, needs a model for each channel, and this flow
does not make one.

## Reading the visibilities

Each island call reads all of the visibilities, which is the main cost of the
flow. wsclean first reorders the data into its own files, and that step can be
shared. The first island of a round saves its reordered files and the rest
reuse them. They can not be kept between rounds, as the data column changes
with each subtraction, or shared with the MFS image, which has a different
number of channels.

## What it costs, and where it is weak

- The visibilities are read once for each island, in each round, in each
  cycle. Fewer rounds, and islands that are skipped once they are clean, are
  what keep this in hand.
- Faint islands are many, and each still costs a full read. A faint island
  also gains little from many channels, as it is below the noise in each of
  them. Imaging close islands in one call, or leaving the faint ones to the MFS
  image, are the likely answers.
- A source outside a window aliases into it. The alias is the accuracy of the
  gridder times the flux of the source, which is far below the true sidelobes
  of that source. Padding the image does not reduce it. Where it lands changes
  with the size of the window, which is one way to tell it from a real source.
- A mask drawn on an artefact stays in every later cycle, and cleaning it puts
  the artefact in the model. Calibration errors around bright sources are the
  likely cause, and no amount of cleaning removes them.
- The weighting assumes the default wsclean padding of 1.2.

## What has been tried

The flow has been run on a single RACS beam, with the brightest twelve islands
over three rounds. The image of what was left improved on the first image. The
cycles had not been run on data when this page was written.

Still to be checked on real data:

- that an island image lines up with the field image pixel for pixel
- that the PSF of an island matches the PSF of the field
- the far sidelobe level, against the assumed `round_gain`
- what eye-patch makes of an image of what is left after a cycle
