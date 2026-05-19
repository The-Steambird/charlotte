from pathlib import Path
from typing import TYPE_CHECKING

from vsdeband import Grainer, deband_detail_mask, placebo_deband
from vsdenoise import DFTTest, MVToolsPreset, Prefilter, bm3d, deblock_qed, mc_degrain, nl_means
from vsjetpack import setup_logging
from vspreview import is_preview
from vssource import BestSource
from vstools import (
    DitherType,
    core,
    depth,
    finalize_clip,
    initialize_clip,
    set_output,
)


if TYPE_CHECKING:
    from vapoursynth import VideoNode

setup_logging()


def filter_chain(input_path: Path, preview: bool = False) -> tuple[VideoNode, ...] | VideoNode:
    clip = initialize_clip(clip=BestSource(show_pretty_progress=True).source(input_path), bits=16)

    # Deblock
    deblock = deblock_qed(clip, quant=(24, 0), alpha=(1, 1), beta=(2, 2), chroma_mode=0)

    # Denoise
    ref = mc_degrain(
        clip=deblock,
        tr=3,
        blksize=32,
        refine=2,
        thsad=150,
        prefilter=Prefilter.DFTTEST(backend=DFTTest.Backend.NVRTC(num_streams=4)),
        preset=MVToolsPreset.HQ_SAD,
    )
    denoise = bm3d(
        clip=deblock,
        sigma=0.6,
        tr=2,
        profile=bm3d.Profile.NORMAL,
        ref=ref,
        planes=0,
        backend=bm3d.Backend.CUDA_RTC,
    )
    denoise = nl_means(
        clip=denoise, h=0.2, tr=2, ref=ref, planes=[1, 2], backend=nl_means.Backend.CUDA
    )

    # Deband
    deband_mask = deband_detail_mask(clip=denoise, sigma=1.0, brz=(0.01, 0.02))
    deband_mask = deband_mask.std.Maximum().std.BoxBlur(hradius=2, vradius=2)
    deband = placebo_deband(clip=denoise, radius=16, thr=2, grain=0, iterations=3)
    merge = core.std.MaskedMerge(deband, denoise, deband_mask)

    # Grain
    grain = Grainer.FBM_SIMPLEX(
        merge,
        strength=(0.4, 0),
        static=False,
        temporal=(0.25, 2),
        luma_scaling=5,
        size=1.0,
        seed=333,
    )

    # Output
    final = finalize_clip(clip=grain, bits=10)

    if preview:
        return clip, deblock, denoise, deband_mask, merge, grain, final
    return final


if __name__ in {"__main__", "__vapoursynth__", "__vspreview__"}:
    file_name = Path(__file__).stem
    file_path = Path(__file__).parent.parent / "output" / file_name / f"{file_name}.ivf"

    clip, deblock, denoise, deband_mask, merge, grain, final = filter_chain(file_path, preview=True)

    if is_preview():
        set_output(depth(clip, 8, dither_type=DitherType.NONE), "Source")
        set_output(deblock, "Deblock")
        set_output(depth(denoise, 8, dither_type=DitherType.NONE), "Denoised")
        set_output(core.akarin.Expr([denoise, clip], ["x y - 8 * 32768 +"]), "Denoise Diff")
        set_output(deband_mask, "Deband Mask")
        set_output(merge, "Deband")
        set_output(grain, "Grained")
        set_output(final, "Filtered")
    else:
        set_output(final)
