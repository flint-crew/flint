(selfcal)=
# Continuum imaging and self-calibration

`flint` allows for a very flexible, but powerful, set of self-calibration options. The full suite of options are enumerated below and can be run using the `flint_flow_continuum_pipeline` entry point.

As a user, you will need to set and consider some of the following options:

- `science_path`: Directory containing your input science visibilities
- `split-path`: Where your output calibrated visibiltiies will be placed
- `calibrated-bandpass-path`: Set either where your bandpass solutions are (see {ref}`bandpass calibration <bandpass>`).
- `imaging-strategy`: Path to your 'strategy' YAML file (see {ref}`below <strategy>`).

Note that `flint` supports passing in a config file to specify CLI options via `--cli-config` (see {ref}`config`). This is particularly useful for sharing a common set of options between multiple runs of the pipeline.

## Skipping bandpass calibration

`flint` supports the imaging of CASDA deposited measurement sets whose visibilities produced by the operational ASKAP pipeline. These measurement sets are already bandpass calibrated, and often have gone through multiple rounds of self-calibration. In such a situation bandpass solutions are not needed. Should `flint` detect that the measurement sets specified by `science_path` be appear to be from CASDA, the `flint_flow_continuum_pipeline` will not attempt to apply any bandpass set of solutions, and will appropriately pre-process the visibilities accordingly.

(strategy)=
## Imaging strategy

To keep track of options across rounds of self-calibration we use a 'strategy' file in a YAML format. We give details of this in {ref}`config`. You can generate a minimal strategy file using `flint_config create`. You can specify a 'global' set of options under `defaults`, which will be overwritten by any options set in rounds of `selfcal`.

By way of example, the following strategy file appears to work well for RACS-style data. We do not recommend using this verbatim for any/all sets of data. The `flint_flow_selfcal_pipeline` workflow is referenced by the `selfcal` operation.

```yaml
version: 0.2
defaults:
  wsclean:
    temp_dir: $MEMDIR
    abs_mem: 100
    local_rms_window: 30
    size: 6128
    local_rms: true
    force_mask_rounds: 10
    auto_mask: 10
    auto_threshold: 0.75
    threshold: null
    channels_out: 18
    mgain: 0.7
    nmiter: 14
    niter: 200000
    multiscale: true
    multiscale_scale_bias: 0.6
    multiscale_scales: !!python/tuple
    - 0
    - 4
    - 8
    - 16
    - 24
    - 32
    - 48
    - 64
    - 92
    - 128
    - 196
    fit_spectral_pol: 5
    weight: briggs 0.5
    data_column: CORRECTED_DATA
    scale: 2.5asec
    gridder: wgridder
    nwlayers: null
    wgridder_accuracy: 0.0001
    join_channels: true
    minuv_l: 200
    minuvw_m: null
    maxw: null
    no_update_model_required: false
    no_small_inversion: false
    beam_fitting_size: 1.25
    fits_mask: null
    deconvolution_channels: 6
    parallel_gridding: 36
    pol: i
  gaincal:
    solint: 60s
    calmode: p
    round: 0
    minsnr: 0.0
    uvrange: '>235m'
    selectdata: true
    gaintype: G
    nspw: 1
  masking:
    base_snr_clip: 4
    flood_fill: true
    flood_fill_positive_seed_clip: 6
    flood_fill_positive_flood_clip: 1.25
    flood_fill_use_mbc_adaptive_max_depth: 4
    flood_fill_use_mbc_adaptive_skew_delta: 0.025
    flood_fill_use_mbc_adaptive_step_factor: 4
    grow_low_snr_island: false
    grow_low_snr_island_clip: 1.75
    grow_low_snr_island_size: 12046
  archive:
    tar_file_re_patterns: !!python/tuple
    - .*round4.*MFS.*(image|residual|model,cube)\.fits
    - .*linmos.*
    - .*weight\.fits
    - .*yaml
    - .*\.txt
    - .*png
    - .*beam[0-9]+\.ms\.(zip|tar)
    - .*beam[0-9]+\.ms
    - .*\.caltable
    - .*\.tar
    - .*\.csv
    - .*comp\.fits
    copy_file_re_patterns: !!python/tuple
    - .*linmos.*fits
    - .*weight\.fits
    - .*png
    - .*csv
    - .*caltable\.tar
    - .*txt
    - .*comp\.fits
    - .*yaml
selfcal:
  0:
    wsclean:
      auto_mask: 8
      auto_threshold: 3
      multiscale_scale_bias: 0.8
  1:
    wsclean:
      auto_mask: 5
      auto_threshold: 1.5
      force_mask_rounds: 5
      local_rms: False
      nmiter: 9
    gaincal:
      solint: 60s
      calmode: p
      uvrange: '>400m'
      nspw: 2
    masking:
      flood_fill_use_mbc: true
      flood_fill_positive_seed_clip: 1.5
      flood_fill_positive_flood_clip: 1.2
      flood_fill_use_mbc_box_size: 400
  2:
    wsclean:
      auto_mask: 2
      auto_threshold: 1.0
      force_mask_rounds: 10
      local_rms: false
      nmiter: 11
    gaincal:
      solint: 30s
      calmode: p
      uvrange: '>400m'
      nspw: 4
    masking:
      flood_fill_use_mbc: true
      flood_fill_positive_seed_clip: 1.2
      flood_fill_positive_flood_clip: 1.1
      flood_fill_use_mbc_box_size: 300
  3:
    wsclean:
      auto_mask: 2.0
      auto_threshold: 0.5
      force_mask_rounds: 10
      local_rms: false
      nmiter: 16
    gaincal:
      solint: 480s
      calmode: ap
      uvrange: '>400m'
      nspw: 2
    masking:
      flood_fill_use_mbc: true
      flood_fill_positive_seed_clip: 1.2
      flood_fill_positive_flood_clip: 0.8
      flood_fill_use_mbc_box_size: 60
  4:
    wsclean:
      auto_mask: 2.0
      auto_threshold: 0.5
      force_mask_rounds: 10
      local_rms: False
    gaincal:
      solint: 480s
      calmode: ap
      uvrange: '>400m'
      nspw: 2
    masking:
      flood_fill_use_mbc: true
      flood_fill_positive_seed_clip: 1.2
      flood_fill_positive_flood_clip: 0.7
      flood_fill_use_mbc_box_size: 60
stokesv:
  wsclean:
    pol: v
    no_update_model_required: true
    nmiter: 6
```

## Other notes

Should `--stokes-v-imaging` be invoked than after the last round of self-calibration each measurement set will be imaged in Stokes V. Settings around the imaging parameters for the Stokes V imaging are specified by the `stokesv` operation.

Should `--coadd-cubes` be invoked than the spectral Stokes-I cubes produced by `wsclean` after the final imaging round are co-addede together to form a field image at different channel ranges. This can be used to investigate the spectral variation of sources. Each channel will be convolved to a common resolution for that channel. In this mode a single `linmos` task is invoked to do the co-adding, which may mean a single long running task should `wsclean` produce many output channels. Be mindful of memory requirements here, as this modde of operation will attempt to load the entirety of all cubes and weights into memory.

## Accessing via the CLI

The primary entry point for the continuum and self-calibration and imaging pipeline in `flint` is the `flint_flow_continuum_pipeline`:

```{argparse}
:ref: flint.prefect.flows.continuum_pipeline.get_parser
:prog: flint_flow_continuum_pipeline
```

## The `FieldOptions` class

Embedded below is the `flint` `Options` class used to drive the `flint_flow_continuum_pipeline` workflow. Input values are validated by `pydantic` to ensure they are appropriately typed.

```{literalinclude}  ../../flint/options.py
:pyobject: FieldOptions
```
