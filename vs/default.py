from pathlib import Path
from typing import TYPE_CHECKING

from preview import compare, show
from vsdeband import Grainer, deband_detail_mask, placebo_deband
from vsdenoise import deblock_qed
from vsjetpack import setup_logging
from vssource import BestSource
from vstools import core, finalize_clip, initialize_clip


if TYPE_CHECKING:
    from vapoursynth import VideoNode

setup_logging()


def filter_chain(input_path: Path) -> VideoNode:
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
    merge = core.std.MaskedMerge(clipa=deband, clipb=deblock, mask=detail_mask)

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

    # Preview (vsview only)
    show(clip, "Source")
    compare("Deblock", clip, deblock)
    show(detail_mask, "Detail Mask")
    compare("Deband", deblock, deband)
    show(merge, "Merge")
    show(grain, "Grained")
    show(final, "Filtered")

    return final


if __name__ in {"__main__", "__vapoursynth__", "__vsview__"}:
    stem = globals().get("stem") or Path(__file__).stem
    final = filter_chain(Path(__file__).parent.parent / "output" / stem / f"{stem}.ivf")

    if __name__ != "__vsview__":
        final.set_output()
