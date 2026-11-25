"""Spec for the Slack GIF validator's frame walk.

These tests fail while validate_gif / is_slack_ready seek through every frame
of a caller-supplied GIF with no cap. They pass once a walk past a few
hundred frames stops and returns a validation error instead of finishing
the scan.

The bomb used here is a real GIF: a 128x128 logical screen (acceptable
Slack emoji dimensions) and one image descriptor per frame, each a 1x1
LZW block. Pillow 10.0.0 and 13.0.0 both report the next frame only by
reading it (GifImageFile._seek raises EOFError at the trailer; _seek_check
does not apply an upper bound while _n_frames is still None), so the
validator's seek loop is what visits every descriptor.
"""

from __future__ import annotations

import struct
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from core.validators import is_slack_ready, validate_gif  # noqa: E402

# Slack emoji GIFs are a few seconds long. A few hundred frames is the most
# the validator may visit; the extra slots cover Image.open's first seek and
# a single lookahead that discovers the cap.
MAX_FRAME_VISITS = 400
MAX_WALK_SECONDS = 1.0
# Far past the cap. Counts are large enough that Pillow 13's seek+decode loop
# stays over MAX_WALK_SECONDS: about 4s for a 128px emoji screen at 200k
# frames, and about 4s for a 480px message screen at 40k frames.
EMOJI_BOMB_FRAMES = 200_000
MESSAGE_BOMB_FRAMES = 40_000
SHORT_FRAMES = 12


def gif_with_frames(n_frames: int, width: int, height: int) -> bytes:
    """Build a GIF whose frame count is n_frames and whose screen is width x height.

    Each frame is a 1x1 image descriptor. The screen size, not the descriptor,
    is what the validator reads as the GIF dimensions.
    """
    if not 1 <= n_frames <= 5_000_000:
        raise ValueError(n_frames)
    if not 1 <= width <= 65535 or not 1 <= height <= 65535:
        raise ValueError((width, height))

    out = bytearray(b"GIF89a")
    out += struct.pack("<HH", width, height)
    # Global color table, 2 entries.
    out += bytes([0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF])
    # Image separator, origin, 1x1, no local table, minimal LZW image data.
    frame = bytes([0x2C]) + struct.pack("<HHHH", 0, 0, 1, 1)
    frame += bytes([0x00, 0x02, 0x02, 0x44, 0x01, 0x00])
    out += frame * n_frames
    out += b"\x3B"
    return bytes(out)


@contextmanager
def counting_gif_seeks():
    """Count GifImageFile._seek calls, which is the per-frame walk.

    n_frames and Image.seek both end in _seek. Image.open of a GIF calls it
    once for frame 0. The wrapper always delegates to Pillow's implementation.
    """
    from PIL.GifImagePlugin import GifImageFile

    calls = {"n": 0}
    original = GifImageFile._seek

    def wrapped(self, frame, update_image=True):
        calls["n"] += 1
        return original(self, frame, update_image)

    GifImageFile._seek = wrapped
    try:
        yield calls
    finally:
        GifImageFile._seek = original


class FrameScanLimitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, n_frames: int, size: int) -> Path:
        path = self.tmp / name
        path.write_bytes(gif_with_frames(n_frames, size, size))
        return path

    def test_short_emoji_gif_is_accepted(self):
        path = self._write("short-emoji.gif", SHORT_FRAMES, 128)
        with counting_gif_seeks() as calls:
            passed, info = validate_gif(path, is_emoji=True, verbose=False)
        self.assertTrue(passed, info)
        self.assertEqual(info["frame_count"], SHORT_FRAMES)
        self.assertNotIn("error", info)
        # One _seek per frame, plus the seek that hits the trailer.
        self.assertGreaterEqual(calls["n"], SHORT_FRAMES)
        self.assertLessEqual(calls["n"], SHORT_FRAMES + 5)
        self.assertTrue(is_slack_ready(path, is_emoji=True, verbose=False))

    def test_short_message_gif_is_accepted(self):
        path = self._write("short-message.gif", SHORT_FRAMES, 480)
        passed, info = validate_gif(path, is_emoji=False, verbose=False)
        self.assertTrue(passed, info)
        self.assertEqual(info["frame_count"], SHORT_FRAMES)
        self.assertTrue(is_slack_ready(path, is_emoji=False, verbose=False))

    def _assert_rejected_without_full_walk(self, kind: str, is_emoji: bool):
        # Dimensions that already satisfy the Slack check for this mode, so a
        # pass/fail result is about the frame walk and not about width/height.
        if is_emoji:
            screen, declared = 128, EMOJI_BOMB_FRAMES
        else:
            screen, declared = 480, MESSAGE_BOMB_FRAMES
        path = self._write(f"bomb-{kind}-{is_emoji}.gif", declared, screen)
        started = time.perf_counter()
        with counting_gif_seeks() as calls:
            if kind == "validate_gif":
                passed, info = validate_gif(path, is_emoji=is_emoji, verbose=False)
            else:
                passed = is_slack_ready(path, is_emoji=is_emoji, verbose=False)
                info = None
        elapsed = time.perf_counter() - started
        problems = []
        if calls["n"] > MAX_FRAME_VISITS:
            problems.append(
                f"visited {calls['n']} frames (limit {MAX_FRAME_VISITS}); "
                f"file declares {declared}"
            )
        if elapsed >= MAX_WALK_SECONDS:
            problems.append(
                f"took {elapsed:.2f}s (limit {MAX_WALK_SECONDS:.1f}s) "
                f"for a {path.stat().st_size} byte GIF"
            )
        if passed:
            problems.append("reported the GIF as meeting Slack limits")
        if kind == "validate_gif":
            if "error" not in info:
                problems.append(f"did not return a validation error: {info!r}")
            reported = info.get("frame_count")
            if reported == declared:
                problems.append("reported the full attacker-controlled frame count")
        self.assertFalse(
            problems,
            "uncapped GIF frame scan in "
            + kind
            + ("" if is_emoji else " (message GIF)")
            + ": "
            + "; ".join(problems),
        )

    def test_validate_gif_rejects_emoji_bomb_without_walking_every_frame(self):
        self._assert_rejected_without_full_walk("validate_gif", is_emoji=True)

    def test_validate_gif_rejects_message_bomb_without_walking_every_frame(self):
        self._assert_rejected_without_full_walk("validate_gif", is_emoji=False)

    def test_is_slack_ready_rejects_emoji_bomb_without_walking_every_frame(self):
        self._assert_rejected_without_full_walk("is_slack_ready", is_emoji=True)

    def test_is_slack_ready_rejects_message_bomb_without_walking_every_frame(self):
        self._assert_rejected_without_full_walk("is_slack_ready", is_emoji=False)


if __name__ == "__main__":
    unittest.main()
