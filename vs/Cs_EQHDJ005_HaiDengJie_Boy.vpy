from pathlib import Path
from typing import Any

from vsaa import based_aa
from vsdeband import Grainer, deband_detail_mask, placebo_deband
from vsdehalo import fine_dehalo
from vsdenoise import MVToolsPreset, Prefilter, bm3d, mc_degrain, nl_means
from vsjetpack import setup_logging
from vspreview import is_preview
from vssource import BestSource
from vstools import core, finalize_clip, initialize_clip, set_output, depth, DitherType

logger = setup_logging()


def filter_chain(input_path: Path) -> Any:
    clip = initialize_clip(
        clip=BestSource(show_pretty_progress=True).source(input_path), bits=16
    )

    # Protective denoise
    ref = mc_degrain(
        clip=clip,
        tr=1,
        blksize=16,
        refine=1,
        thsad=120,
        prefilter=Prefilter.DFTTEST(),
        preset=MVToolsPreset.HQ_SAD,
    )
    denoise = bm3d(
        clip=clip,
        sigma=0.4,
        tr=1,
        profile=bm3d.Profile.NORMAL,
        ref=ref,
        planes=0,
    )
    denoise = nl_means(clip=denoise, h=0.2, tr=2, ref=ref, planes=[1, 2])

    # Anti-aliasing
    aa = based_aa(denoise, rfactor=2.0)

    # Dehalo
    dehalo = fine_dehalo(aa, rx=1, ry=1, darkstr=0.0, brightstr=0.5)

    # Deband
    deband_mask = deband_detail_mask(clip=dehalo, sigma=1.0, brz=(0.015, 0.035))
    deband_mask = deband_mask.std.Maximum().std.BoxBlur(hradius=2, vradius=2)
    deband = placebo_deband(clip=dehalo, radius=24, thr=2.5, grain=0, iterations=2)
    merge = core.std.MaskedMerge(deband, dehalo, deband_mask)

    # Grain
    grain = Grainer.SIMPLEX(
        merge,
        strength=(0.2, 0.02),
        static=False,
        temporal=(0.25, 2),
        luma_scaling=6,
        size=1.0,
        seed=333,
    )

    # Output
    final = finalize_clip(clip=grain, bits=10)

    return clip, denoise, aa, deband_mask, merge, grain, final


file_name = Path(__file__).stem
file_path = Path(__file__).parent.parent / "output" / file_name / f"{file_name}.mkv"

clip, denoise, aa, deband_mask, merge, grain, final = filter_chain(file_path)

if is_preview():
    set_output(depth(clip, 8, dither_type=DitherType.NONE), "Source")
    set_output(depth(denoise, 8, dither_type=DitherType.NONE), "Denoised")
    set_output(core.akarin.Expr([denoise, clip], ["x y - 8 * 32768 +"]), "Denoise Diff")
    set_output(aa, "Anti-Aliased")
    set_output(deband_mask, "Deband Mask")
    set_output(merge, "Deband")
    set_output(grain, "Grained")
    set_output(final, "Filtered")
else:
    set_output(final)
