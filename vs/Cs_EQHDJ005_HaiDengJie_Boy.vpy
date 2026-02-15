from pathlib import Path
from typing import Any

from vsaa import EEDI3, based_aa
from vsdeband import deband_detail_mask, placebo_deband
from vsdehalo import fine_dehalo
from vsdenoise import MVToolsPreset, Prefilter, bm3d, mc_degrain, nl_means
from vsjetpack import setup_logging
from vspreview import is_preview
from vssource import BestSource
from vstools import core, finalize_clip, initialize_clip, set_output
from vodesfunc import adaptive_grain


logger = setup_logging()


def filter_chain(input_path: Path) -> Any:
    clip = initialize_clip(
        clip=BestSource(show_pretty_progress=True).source(input_path), bits=16
    )

    # Denoise
    ref = mc_degrain(
        clip=clip,
        tr=2,
        blksize=32,
        refine=2,
        thsad=400,
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
        backend=bm3d.Backend.CUDA_RTC,
    )
    denoise = nl_means(
        clip=denoise, h=0.2, tr=2, ref=ref, planes=[1, 2], backend=nl_means.Backend.CUDA
    )

    # Anti-aliasing
    eedi3 = EEDI3(alpha=0.1, beta=0.4, gamma=40, nrad=3, mdis=20)
    aa = based_aa(denoise, antialiaser=eedi3, rfactor=2.0)

    # Dehalo
    dehalo = fine_dehalo(aa, rx=1, ry=1, darkstr=0.0, brightstr=0.4)

    # Deband
    deband_mask = deband_detail_mask(clip=dehalo, sigma=1.0, brz=(0.015, 0.035))
    deband_mask = deband_mask.std.Maximum().std.BoxBlur(hradius=2, vradius=2)
    deband = placebo_deband(clip=dehalo, radius=32, thr=2.5, grain=0, iterations=2)
    merge = core.std.MaskedMerge(deband, dehalo, deband_mask)

    # Grain
    grain = adaptive_grain(
        clip=merge, strength=[0.2, 0.05], size=1.0, type=2, luma_scaling=8, static=False
    )

    # Output
    final = finalize_clip(clip=grain, bits=10)

    return clip, final


file_path = (
    Path(__file__).parent.parent
    / "output"
    / "Cs_EQHDJ005_HaiDengJie_Boy"
    / "Cs_EQHDJ005_HaiDengJie_Boy.mkv"
)
clip, final = filter_chain(file_path)

if is_preview():
    set_output(clip, "Source")
    set_output(final, "Filtered")
else:
    set_output(final)
