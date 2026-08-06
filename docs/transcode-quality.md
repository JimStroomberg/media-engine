# Transcode quality and resolution profiles

Media Engine exposes two independent controls for video output:

- `quality` selects the output canvas: 360p, 480p, 720p, 1080p, 1440p, or 2160p.
- `quality_profile` selects the compression envelope: `compact`, `balanced`, or `high`.

Keeping these controls separate lets a client request, for example, a compact 1080p delivery file or a high-quality 720p
archive without knowing which encoder is installed on the worker.

## Resolution and codec selection

| `quality` | Output canvas | Default codec for unknown inputs |
| --- | ---: | --- |
| `low_360p` | 640x360 | H.264 |
| `sd_480p` | 848x480 | H.264 |
| `hd_720p` | 1280x720 | H.264 |
| `fhd_1080p` | 1920x1080 | H.264 |
| `qhd_1440p` | 2560x1440 | H.265 |
| `uhd_2160p` | 3840x2160 | H.265 |

`quality=auto` chooses the highest suitable canvas. An explicit canvas above the source is downgraded to the closest
available preset. Inputs below the smallest preset use the 360p canvas. Scaling preserves aspect ratio and pads the
remaining canvas when necessary.

`codec=auto` preserves an H.264 or H.265 source codec. For other source codecs it uses the resolution preset's default
from the table. Clients can explicitly request `h264` or `h265` when compatibility or storage efficiency matters more
than preserving the source codec.

`quality=audio_only` extracts AAC audio and does not use a video quality profile.

## Compression profiles

All three profiles use constrained variable bitrate. The table contains the `average / peak / buffer` values for the
`balanced` profile in megabits per second.

| Resolution | H.264 Mbps | H.265 Mbps |
| --- | ---: | ---: |
| 360p | 1.00 / 1.50 / 2.00 | 0.75 / 1.10 / 1.50 |
| 480p | 1.75 / 2.50 / 3.50 | 1.25 / 1.80 / 2.50 |
| 720p | 3.50 / 5.00 / 7.00 | 2.50 / 3.50 / 5.00 |
| 1080p | 6.00 / 8.00 / 12.00 | 4.50 / 6.00 / 9.00 |
| 1440p | 10.00 / 14.00 / 20.00 | 7.50 / 10.00 / 15.00 |
| 2160p | 16.00 / 22.00 / 32.00 | 12.00 / 16.00 / 24.00 |

The selected profile scales all three values together:

| `quality_profile` | Relative budget | Intended use |
| --- | ---: | --- |
| `compact` | 0.67x | Previews, chat delivery, bandwidth-sensitive clients |
| `balanced` | 1.00x | General-purpose default |
| `high` | 1.50x | Difficult motion, grain, or quality-sensitive output |

Values are encoder targets, not promised file sizes. Hardware encoders make different decisions for the same content,
so output bytes, instantaneous bitrate, and objective score can differ between CPU, RKMPP, and NVIDIA workers. The
contract is a common rate-control and quality envelope, not byte-identical output. A compatible MP4 that already fits the
requested canvas and codec can be remuxed without re-encoding; in that case `quality_profile` does not alter its media
bytes.

## Backend mapping

- CPU workers use x264/x265 with the shared average, peak, and buffer values.
- RK1 workers use RKMPP constrained VBR, H.264 High or H.265 Main, and the shared rate envelope. The software scaler is
  intentional: RGA scaling was much faster in the benchmark but lost roughly nine VMAF points on detailed 4K
  downscaling, so it is not used for these public quality profiles.
- Xavier NX workers use NVDEC, VIC 10-tap scaling, NVIDIA VBR with an explicit peak bitrate, and H.264 High or H.265
  Main. `maxperf-enable` avoids encoder clock throttling during a job.

The NVIDIA property mapping follows the
[Jetson Linux R35.6.1 accelerated GStreamer guide](https://docs.nvidia.com/jetson/archives/r35.6.1/DeveloperGuide/SD/Multimedia/AcceleratedGstreamer.html):
`control-rate=0` is VBR, `peak-bitrate` bounds VBR bursts, H.264 profile `4` is High, and VIC interpolation method `3`
is the 10-tap filter.

## API examples

Pipeline version 2 is the current transcode contract:

```json
{
  "asset_id": "<asset-id>",
  "pipeline": "transcode",
  "pipeline_version": "2",
  "options": {
    "quality": "fhd_1080p",
    "codec": "h265",
    "quality_profile": "balanced"
  }
}
```

Omitting `pipeline_version` selects the latest version. Omitting `quality_profile` selects `balanced`. Transcode version
1 remains addressable for older clients and normalizes to balanced behavior, but it does not accept the new option.
Changing any normalized option produces a different deterministic run identity, so compact, balanced, and high outputs
cannot collide in the reusable cache.

The temporary multipart `/jobs` endpoint accepts the same `quality`, `codec`, and `quality_profile` values. New clients
should use platform v2.

## Hardware verification

The profile set was verified on 2026-08-06 with isolated, warmed RK1 and Jetson Xavier NX 16 GB workers. Three
approximately 40-second samples covered detailed 4K nature footage, clean 1080p animation, and dark/grainy 1080p live
action. The 4K sample was encoded to 1080p; the two 1080p samples were encoded to 720p. VMAF references were scaled to
the target canvas before comparison.

Balanced profile results:

| Content | Codec | RK1 video Mbps / VMAF | Xavier NX video Mbps / VMAF |
| --- | --- | ---: | ---: |
| Detailed 4K to 1080p | H.264 | 6.025 / 84.061 | 5.796 / 90.716 |
| Detailed 4K to 1080p | H.265 | 4.547 / 85.867 | 3.712 / 89.604 |
| Animation to 720p | H.264 | 3.815 / 88.084 | 3.460 / 90.661 |
| Animation to 720p | H.265 | 2.671 / 88.609 | 2.085 / 90.366 |
| Dark/grainy live action to 720p | H.264 | 4.175 / 93.694 | 3.615 / 95.960 |
| Dark/grainy live action to 720p | H.265 | 2.903 / 95.618 | 2.232 / 95.560 |

The difficult 4K-to-1080p H.265 sample also verified the complete tier ladder:

| Worker | Profile | File size | Video Mbps | VMAF | Encode time |
| --- | --- | ---: | ---: | ---: | ---: |
| RK1 | compact | 16.4 MB | 3.138 | 84.186 | 22.55 s |
| RK1 | balanced | 23.5 MB | 4.547 | 85.874 | 22.65 s |
| RK1 | high | 34.7 MB | 6.801 | 86.852 | 23.01 s |
| Xavier NX | compact | 13.1 MB | 2.480 | 87.916 | 21.25 s |
| Xavier NX | balanced | 19.2 MB | 3.712 | 89.604 | 21.48 s |
| Xavier NX | high | 28.4 MB | 5.552 | 90.656 | 21.54 s |

Every step increased size and VMAF on both workers without a meaningful throughput regression. NVIDIA H.264 High plus
10-tap scaling also improved the detailed sample while producing a smaller file than the previous CBR/Baseline mapping.
RKMPP AVBR, quantizer caps, Lanczos scaling, and RGA scaling were separately tested and rejected because they either did
not improve quality or fell outside the intended quality envelope.

One RKMPP timing detail matters when reproducing these numbers: its MP4 output duplicated an initial frame for the 4K
sample. The distorted and reference streams were aligned before VMAF calculation. Comparing them without temporal
alignment produced a false score near 40 despite visually correct output.
