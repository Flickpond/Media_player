"""Bake the worker's colour transforms for the WebGL preview (no npm build).

Run from the repository root with Python and FFmpeg, or pass
--container flickpond-worker-1 to use the local worker's FFmpeg.
Only development uses FFmpeg: the browser samples the resulting small PNG.
"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.services.media_rules import Frame  # noqa: E402
from app.worker.edit_ops.preset import build  # noqa: E402

SIZE = 32
NAMES = ("bw", "warm", "cool", "vintage", "vivid")
TILE = 8


def generate(command: list[str], destination: Path) -> None:
    # x = green * SIZE + red; y = blue. Each look occupies SIZE rows.
    # Sample the centre of solid 8x8 tiles, so the worker's actual 4:2:0
    # conversion cannot mix the colours of neighbouring lookup points.
    cube = b"".join(
        b"".join(
            bytes(round(channel * 255 / (SIZE - 1)) for channel in (red, green, blue)) * TILE
            for green in range(SIZE)
            for red in range(SIZE)
        )
        * TILE
        for blue in range(SIZE)
    )

    def ffmpeg(
        pixels: bytes,
        height: int,
        output: list[str],
        filters: str = "null",
        width: int = SIZE * SIZE,
    ):
        return subprocess.run(
            [
                *command,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-s",
                f"{width}x{height}",
                "-i",
                "pipe:0",
                "-vf",
                filters,
                "-frames:v",
                "1",
                *output,
                "pipe:1",
            ],
            input=pixels,
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout

    chains = {name: build({"name": name}, Frame(None, None)).video for name in NAMES}
    looks = []
    width = SIZE * SIZE * TILE
    for name in NAMES:
        transformed = ffmpeg(
            cube,
            SIZE * TILE,
            ["-f", "rawvideo", "-pix_fmt", "rgb24"],
            ",".join([*chains[name], "format=rgb24"]),
            width=width,
        )
        looks.append(
            b"".join(
                transformed[offset : offset + 3]
                for blue in range(SIZE)
                for green in range(SIZE)
                for red in range(SIZE)
                for offset in [
                    ((blue * TILE + TILE // 2) * width + (green * SIZE + red) * TILE + TILE // 2)
                    * 3
                ]
            )
        )
    png = ffmpeg(
        b"".join(looks),
        SIZE * len(NAMES),
        ["-f", "image2pipe", "-c:v", "png", "-compression_level", "9", "-pred", "mixed"],
    )
    destination.write_bytes(png)
    manifest = {
        "size": SIZE,
        "order": NAMES,
        "filters": chains,
        "sha256": hashlib.sha256(png).hexdigest(),
    }
    destination.with_suffix(".json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"{destination.name}: {len(png):,} bytes; {manifest['sha256']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", help="use FFmpeg inside this running Docker container")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    command = (
        ["docker", "exec", "-i", args.container, "ffmpeg"] if args.container else [args.ffmpeg]
    )
    generate(command, ROOT / "frontend" / "preset-luts.png")
