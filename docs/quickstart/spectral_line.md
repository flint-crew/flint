# Spectral line imaging

`flint` has the beginnings of a spectral line workflow that operates against continuum-subtracted visibilities. This mode requires that continuum imaging has been performed using `wsclean`'s `--save-source-list` option, which outputs a text file describing each clean component constructed by `wsclean` and its parameterisation (e.g. location and spatial/spectral shape).

This workflow will first generate model visibilities at the spectral resolution of the data (note that `wsclean` will generate model visibilites that are constant of sub-band intervals). There are two ways to do this:

- using `addmodel` from the `calibrate` container
- using the `crystalball` python package

When using the `addmodel` approach a sub-flow is created, allowing a set of different computing resources to be described. `addmodel` using threads to accelerate compute, which it does extremely well. However it is not able to spread across nodes to achieve higher throughput. By specifying a different compute profile a large set of resources with more cpus on a single node may be specified, thereby speeding up the model prediction.

Alternatively, `crystalball` uses `dask` to achieve parallelism. Since `flint` configures `prefect` to use `dask` as its compute backend, `crystalball` is able to use the same infrastructure. This allows the model prediction process to seamlessly scale across many nodes. For most intents and purposes this `crystalball` approach should be preferred.

## Achieving parallelism

After a set of model visibilities have been predicted for each measurement set, the result of the continuum subtract leaves what should be noise across all channels (of course, sharp spectral features should also remain). Since there is no benefit to attempt to use the whole bandwidth to maximise source sensitivity (e.g. MFS imaging) each individual channel may be image in isolation from one another. With this in mind the general approach is to configure `dask` to allocate many workers that individually use a small set of compute resources.

A field image is produced at each channel by a separate `linmos` process. That is to say, if there are 288 channels in the collection of measurement sets, there will be 288 separate innvokations of `linmos` throughout the flow. Once all field images have been proceduce they are concatenated together (in an asyncrohnous and memory efficient way) into a single FITS cube. See the (fitscube python package)[https://github.com/alecthomson/fitscube] for more information.

## Flow CLI

```bash
flint_flow_subtract_cube_pipeline -h
usage: flint_flow_subtract_cube_pipeline [-h] [--cli-config CLI_CONFIG] [--cluster-config CLUSTER_CONFIG] [--subtract-model-data] [--data-column DATA_COLUMN] [--expected-ms EXPECTED_MS] [--imaging-strategy IMAGING_STRATEGY] [--holofile HOLOFILE] [--linmos-residuals] [--beam-cutoff BEAM_CUTOFF]
                                         [--pb-cutoff PB_CUTOFF] [--stagger-delay-seconds STAGGER_DELAY_SECONDS] [--attempt-subtract] [--subtract-data-column SUBTRACT_DATA_COLUMN] [--predict-wsclean-model] [--attempt-addmodel] [--wsclean-pol-mode WSCLEAN_POL_MODE [WSCLEAN_POL_MODE ...]]
                                         [--calibrate-container CALIBRATE_CONTAINER] [--addmodel-cluster-config ADDMODEL_CLUSTER_CONFIG] [--attempt-crystalball] [--crystallball-wsclean-pol-mode CRYSTALLBALL_WSCLEAN_POL_MODE [CRYSTALLBALL_WSCLEAN_POL_MODE ...]] [--row-chunks ROW_CHUNKS]
                                         [--model-chunks MODEL_CHUNKS]
                                         science_path wsclean_container yandasoft_container

This is a workflow to subtract a continuum model and image the channel-wise data Unlike the continuum imaging and self-calibnration pipeline this flow currently expects that all measurement sets are in the flint format, which means other than the naming scheme that they have been been preprocessed
to place them in the IAU frame and have had their fields table updated. That is to say that they have already been preprocessed and fixed.

positional arguments:
  science_path          Path to the directory containing the beam-wise measurement sets

options:
  -h, --help            show this help message and exit
  --cli-config CLI_CONFIG
                        Path to configuration file
  --cluster-config CLUSTER_CONFIG
                        Path to a cluster configuration file, or a known cluster name.

Inputs for SubtractFieldOptions:
  wsclean_container     Path to the container with wsclean
  yandasoft_container   Path to the container with yandasoft
  --subtract-model-data
                        Subtract the MODEL_DATA column from the nominated data column
  --data-column DATA_COLUMN
                        Describe the column that should be imaed and, if requested, have model subtracted from
  --expected-ms EXPECTED_MS
                        The number of measurement sets that should exist
  --imaging-strategy IMAGING_STRATEGY
                        Path to a FLINT imaging yaml file that contains settings to use throughout imaging
  --holofile HOLOFILE   Path to the holography FITS cube that will be used when co-adding beams
  --linmos-residuals    Linmos the cleaning residuals together into a field image
  --beam-cutoff BEAM_CUTOFF
                        Cutoff in arcseconds to use when calculating the common beam to convol to
  --pb-cutoff PB_CUTOFF
                        Primary beam attenuation cutoff to use during linmos
  --stagger-delay-seconds STAGGER_DELAY_SECONDS
                        The delay, in seconds, that should be used when submitting items in batches (e.g. looping over channels)
  --attempt-subtract    Attempt to subtract the model column from the nominated data column
  --subtract-data-column SUBTRACT_DATA_COLUMN
                        Should the continuum model be subtracted, where to store the output
  --predict-wsclean-model
                        Search for the continuum model produced by wsclean and subtract

Inputs for AddModelSubtractFieldOptions:
  --attempt-addmodel    Invoke the ``addmodel`` visibility prediction, including the search for the ``wsclean`` source list
  --wsclean-pol-mode WSCLEAN_POL_MODE [WSCLEAN_POL_MODE ...]
                        The polarisation of the wsclean model that was generated
  --calibrate-container CALIBRATE_CONTAINER
                        Path to the container with the calibrate software (including addmodel)
  --addmodel-cluster-config ADDMODEL_CLUSTER_CONFIG
                        Specify a new cluster configuration file different to the preferred on. If None, drawn from preferred cluster config

Inputs for CrystalBallOptions:
  --attempt-crystalball
                        Attempt to predict the model visibilities using ``crystalball``
  --crystallball-wsclean-pol-mode CRYSTALLBALL_WSCLEAN_POL_MODE [CRYSTALLBALL_WSCLEAN_POL_MODE ...]
                        The polarisation of the wsclean model that was generated
  --row-chunks ROW_CHUNKS
                        Number of rows of input MS that are processed in a single chunk. If 0 it will be set automatically. Default is 0.
  --model-chunks MODEL_CHUNKS
                        Number of sky model components that are processed in a single chunk. If 0 it will be set automatically. Default is 0.

Args that start with '--' can also be set in a config file (specified via --cli-config). Config file syntax allows: key=value, flag=true, stuff=[a,b,c] (for details, see syntax at https://goo.gl/R74nmi). In general, command-line values override config file values which override defaults.
```

# Output data

The principle result of this workshow is a spectral cube of the observed field at the native spectral resolution of the input collection of measurement sets (including the corresponding weight map produced by `linmos`). Intermediary files created throughout the workflow are deleted once they are no longer needede in order to preserve disk space.
