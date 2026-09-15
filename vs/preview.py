import sys

from typing import TYPE_CHECKING

from vstools import core


if TYPE_CHECKING:
    from vapoursynth import VideoNode


def previewing() -> bool:
    return "__vsview__" in sys.modules


def show(clip: VideoNode, name: str) -> None:
    if previewing():
        # Keep it out of the pipeline subprocess.
        from vsview.api import set_output

        set_output(clip, name)


def boost(clip: VideoNode, gain: int = 6) -> VideoNode:
    """Brighten dark areas to make banding steps and noise more visible."""
    return clip.std.Expr(f"x {gain} *")


def diff(before: VideoNode, after: VideoNode, gain: int = 8) -> VideoNode:
    """Amplify difference to see detail losses."""
    return core.std.MakeDiff(before, after).std.Expr(f"x 32768 - {gain} * 32768 +")


def compare(name: str, before: VideoNode, after: VideoNode) -> None:
    """Show boosted view and diff against the input."""
    show(after, name)
    show(boost(after), f"{name} boosted")
    show(diff(before, after), f"{name} diff")
