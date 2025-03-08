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

## Command-line options

```bash
flint_flow_continuum_pipeline -h
# usage: flint_flow_continuum_pipeline [-h] [--cli-config CLI_CONFIG] [--split-path SPLIT_PATH] [--calibrated-bandpass-path CALIBRATED_BANDPASS_PATH] [--cluster-config CLUSTER_CONFIG]
#                                      [--skip-bandpass-check] [--flagger-container FLAGGER_CONTAINER] [--calibrate-container CALIBRATE_CONTAINER] [--casa-container CASA_CONTAINER]
#                                      [--expected-ms EXPECTED_MS] [--wsclean-container WSCLEAN_CONTAINER] [--yandasoft-container YANDASOFT_CONTAINER] [--potato-container POTATO_CONTAINER]
#                                      [--holofile HOLOFILE] [--rounds ROUNDS] [--skip-selfcal-on-rounds SKIP_SELFCAL_ON_ROUNDS] [--zip-ms] [--run-aegean]
#                                      [--aegean-container AEGEAN_CONTAINER] [--no-imaging] [--reference-catalogue-directory REFERENCE_CATALOGUE_DIRECTORY] [--linmos-residuals]
#                                      [--beam-cutoff BEAM_CUTOFF] [--fixed-beam-shape FIXED_BEAM_SHAPE] [--pb-cutoff PB_CUTOFF] [--use-preflagger] [--use-smoothed] [--use-beam-masks]
#                                      [--use-beam-masks-from USE_BEAM_MASKS_FROM] [--use-beam-masks-rounds USE_BEAM_MASKS_ROUNDS] [--imaging-strategy IMAGING_STRATEGY]
#                                      [--sbid-archive-path SBID_ARCHIVE_PATH] [--sbid-copy-path SBID_COPY_PATH] [--rename-ms] [--stokes-v-imaging] [--coadd-cubes]
#                                      [--update-model-data-with-source-list]
#                                      science_path

# A prefect based pipeline that: - will perform bandpass calibration with PKS B1934-638 data, or from a derived sky-model - copy and apply to science field - image and self-calibration the
# science fields - run aegean source finding

# positional arguments:
#   science_path          Path to directories containing the beam-wise science measurementsets that will have solutions copied over and applied.

# options:
#   -h, --help            show this help message and exit
#   --cli-config CLI_CONFIG
#                         Path to configuration file
#   --split-path SPLIT_PATH
#                         Location to write field-split MSs to. Will attempt to use the parent name of a directory when writing out a new MS.
#   --calibrated-bandpass-path CALIBRATED_BANDPASS_PATH
#                         Path to directory containing the uncalibrated beam-wise measurement sets that contain the bandpass calibration source. If None then the '--sky-model-directory'
#                         should be provided.
#   --cluster-config CLUSTER_CONFIG
#                         Path to a cluster configuration file, or a known cluster name.
#   --skip-bandpass-check
#                         Skip checking whether the path containing bandpass solutions exists (e.g. if solutions have already been applied)

# Inputs for FieldOptions:
#   --flagger-container FLAGGER_CONTAINER
#                         Path to the singularity aoflagger container
#   --calibrate-container CALIBRATE_CONTAINER
#                         Path to the singularity calibrate container
#   --casa-container CASA_CONTAINER
#                         Path to the singularity CASA container
#   --expected-ms EXPECTED_MS
#                         The expected number of measurement set files to find
#   --wsclean-container WSCLEAN_CONTAINER
#                         Path to the singularity wsclean container
#   --yandasoft-container YANDASOFT_CONTAINER
#                         Path to the singularity yandasoft container
#   --potato-container POTATO_CONTAINER
#                         Path to the singularity potato peel container
#   --holofile HOLOFILE   Path to the holography FITS cube that will be used when co-adding beams
#   --rounds ROUNDS       Number of required rouds of self-calibration and imaging to perform
#   --skip-selfcal-on-rounds SKIP_SELFCAL_ON_ROUNDS
#                         Do not perform the derive and apply self-calibration solutions on these rounds
#   --zip-ms              Whether to zip measurement sets once they are no longer required
#   --run-aegean          Whether to run the aegean source finding tool
#   --aegean-container AEGEAN_CONTAINER
#                         Path to the singularity aegean container
#   --no-imaging          Whether to skip the imaging process (including self-calibration)
#   --reference-catalogue-directory REFERENCE_CATALOGUE_DIRECTORY
#                         Path to the directory container the reference catalogues, used to generate validation plots
#   --linmos-residuals    Linmos the cleaning residuals together into a field image
#   --beam-cutoff BEAM_CUTOFF
#                         Cutoff in arcseconds to use when calculating the common beam to convol to
#   --fixed-beam-shape FIXED_BEAM_SHAPE
#                         Specify the final beamsize of linmos field images in (arcsec, arcsec, deg)
#   --pb-cutoff PB_CUTOFF
#                         Primary beam attenuation cutoff to use during linmos
#   --use-preflagger      Whether to apply (or search for solutions with) bandpass solutions that have gone through the preflagging operations
#   --use-smoothed        Whether to apply (or search for solutions with) a bandpass smoothing operation applied
#   --use-beam-masks      Construct beam masks from MFS images to use for the next round of imaging.
#   --use-beam-masks-from USE_BEAM_MASKS_FROM
#                         If `use_beam_masks` is True, this sets the round where beam masks will be generated from
#   --use-beam-masks-rounds USE_BEAM_MASKS_ROUNDS
#                         If `use_beam_masks` is True, this sets which rounds should have a mask applied
#   --imaging-strategy IMAGING_STRATEGY
#                         Path to a FLINT imaging yaml file that contains settings to use throughout imaging
#   --sbid-archive-path SBID_ARCHIVE_PATH
#                         Path that SBID archive tarballs will be created under. If None no archive tarballs are created. See ArchiveOptions.
#   --sbid-copy-path SBID_COPY_PATH
#                         Path that final processed products will be copied into. If None no copying of file products is performed. See ArchiveOptions.
#   --rename-ms           Rename MSs throughout rounds of imaging and self-cal instead of creating copies. This will delete data-columns throughout.
#   --stokes-v-imaging    Specifies whether Stokes-V imaging will be carried out after the final round of imagine (whether or not self-calibration is enabled).
#   --coadd-cubes         Co-add cubes formed throughout imaging together. Cubes will be smoothed channel-wise to a common resolution. Only performed on final set of images
#   --update-model-data-with-source-list
#                         Attempt to update a MSs MODEL_DATA column with a source list (e.g. source list output from wsclean)

# Args that start with '--' can also be set in a config file (specified via --cli-config). Config file syntax allows: key=value, flag=true, stuff=[a,b,c] (for details, see syntax at
# https://goo.gl/R74nmi). In general, command-line values override config file values which override defaults.
```

(strategy)=
## Imaging strategy

To keep track of options across rounds of self-calibration we use a 'strategy' file in a YAML format. We give details of this in {ref}`config`. You can generate a minimal strategy file using `flint_config create`. You can specify a 'global' set of options under `defaults`, which will be overwritten by any options set in rounds of `selfcal`.

By way of example, the following strategy file appears to work well for RACS-style data. We do not recommend using this verbatim for any/all sets of data. The `flint_flow_selfcal_pipeline` workflow if referenced by the `selfcal` operation.

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

Should `--stokes-v-imaging` be invoked than after the last round of self-calibration each measurement set will be images in Stokes-V. Settings around the imaging parameters for these Stokes-V imaging are specified by the `stokesv` operation.

Should `--coadd-cubes` be invoked than the spectral Stokes-I cubes produced by `wsclean` after the final imaging round are co-addede together to form a field image at different channel ranges. This can be used to investigate the spectral variation of sources. Each channel will be convolved to a common resolution for that channel. In this mode a single `linmos` task is invoked to do the co-adding, which may mean a single long running task should `wsclean` produce many output channels. Be mindful of memory requirements here, as this modde of operation will attempt to load the entirety of all cubes and weights into memory.
