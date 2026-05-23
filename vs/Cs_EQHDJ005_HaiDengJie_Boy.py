from pathlib import Path
from typing import TYPE_CHECKING

from vsdeband import Grainer, deband_detail_mask, placebo_deband
from vsdenoise import deblock_qed
from vsjetpack import setup_logging
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
    clip = initialize_clip(clip=BestSource(show_pretty_progress=False).source(input_path), bits=16)

    # Deblock
    deblock = deblock_qed(clip, quant=(24, 0), alpha=(1, 1), beta=(2, 2), chroma_mode=0)

    # Deband
    detail_mask = (
        deband_detail_mask(clip=deblock, sigma=1.0, brz=(0.01, 0.015))
        .std.Maximum()
        .std.BoxBlur(hradius=1, vradius=1)
    )
    deband = placebo_deband(clip=deblock, radius=16, thr=2, grain=0, iterations=4)
    merge = core.std.MaskedMerge(clipa=deband, clipb=clip, mask=detail_mask)

    # Grain
    grain = Grainer.FBM_SIMPLEX(
        merge,
        strength=(1, 0.5),
        static=False,
        temporal=(0.3, 2),
        luma_scaling=5,
        size=1.0,
        seed=727,
    )

    # Output
    final = finalize_clip(clip=grain, bits=10)

    if preview:
        return clip, deblock, detail_mask, deband, merge, grain, final
    return final


if __name__ in {"__main__", "__vapoursynth__", "__vspreview__"}:
    from vspreview import is_preview

    file_name = Path(__file__).stem
    file_path = Path(__file__).parent.parent / "output" / file_name / f"{file_name}.ivf"

    clip, deblock, detail_mask, deband, merge, grain, final = filter_chain(file_path, preview=True)

    if is_preview():
        set_output(depth(clip, 8, dither_type=DitherType.NONE), "Source")
        set_output(deblock, "Deblock")
        set_output(detail_mask, "Detail Mask")
        set_output(deband, "Deband")
        set_output(merge, "Merge")
        set_output(grain, "Grained")
        set_output(final, "Filtered")
    else:
        set_output(final)
