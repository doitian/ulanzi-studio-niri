"""Build the manifest+icons ZIP that the D200X firmware consumes.

Replicates the redphx/strmdck workaround for the firmware bug where ZIP
chunks beginning at 1016, 1016+1024, ... must not start with bytes 0x00 or
0x7c (which collide with the packet header). A dummy.txt file is appended
with random bytes until the resulting ZIP avoids the bad offsets.
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import string
import tempfile
import time
import zipfile
from pathlib import Path

from .config import ButtonEntry, LabelConfig
from .icons import render_cached, request_from_button
from .protocol.ulanzi_d200x import BUTTON_GEOMETRY, PACKET_SIZE, PAYLOAD_SIZE

log = logging.getLogger(__name__)

INVALID_CHUNK_BYTES = (b"\x00", b"\x7c")


def _random_string(n: int) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


def _zip_dir(folder: Path, output: Path, *, compress_level: int = 0) -> None:
    method = zipfile.ZIP_STORED if compress_level == 0 else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(output, "w", method, compresslevel=compress_level) as zf:
        # Write dummy.txt first so it is at a predictable position in the zip
        dummy = folder / "dummy.txt"
        if dummy.exists():
            zf.write(dummy, "dummy.txt")
        for root, _dirs, files in os.walk(folder):
            for name in files:
                fp = Path(root) / name
                if fp == dummy:
                    continue
                arc = fp.relative_to(folder).as_posix()
                zf.write(fp, arc)


def _zip_is_safe(blob: bytes) -> bool:
    """Check that no 1024-aligned chunk starts with a header-collision byte.

    The first packet header is 8 bytes; data starts at offset 8. So bytes that
    end up at the start of subsequent OUT packets live at offsets:
        1016, 1016 + 1024, 1016 + 2048, ...
    """
    size = len(blob)
    for offset in range(PAYLOAD_SIZE, size, PACKET_SIZE):
        if blob[offset : offset + 1] in INVALID_CHUNK_BYTES:
            return False
    return True


def build_buttons_zip(
    buttons: list[ButtonEntry],
    label_cfg: LabelConfig,
    *,
    workdir: Path | None = None,
) -> bytes:
    """Build the buttons ZIP blob; safe for the firmware's packet alignment."""
    workdir = workdir or Path(tempfile.mkdtemp(prefix="ulanzi-build-"))
    page_dir = workdir / "page"
    icons_dir = page_dir / "icons"
    if page_dir.exists():
        shutil.rmtree(page_dir)
    icons_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, dict] = {}
    by_pos = {b.pos: b for b in buttons}

    for pos, geom in BUTTON_GEOMETRY.items():
        button = by_pos.get(pos)
        view: dict = {}
        if button is not None:
            if button.label:
                view["Text"] = button.label
            if button.icon:
                req = request_from_button(
                    button.label, button.icon, geom.width, geom.height, label_cfg
                )
                cached = render_cached(req)
                arc_name = f"{cached.name}"
                target = icons_dir / arc_name
                if not target.exists():
                    shutil.copyfile(cached, target)
                view["Icon"] = f"icons/{arc_name}"
        manifest[f"{geom.col}_{geom.row}"] = {
            "State": 0,
            "ViewParam": [view],
        }

    (page_dir / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), indent=2)
    )

    dummy_path = page_dir / "dummy.txt"
    out_path = workdir / "build.zip"

    dummy_str = ""
    retries = 0
    while True:
        if retries > 0:
            dummy_str += _random_string(8 * retries)
            dummy_path.write_text(dummy_str)
        _zip_dir(page_dir, out_path, compress_level=1)
        blob = out_path.read_bytes()
        if _zip_is_safe(blob):
            break
        retries += 1
        if retries > 64:
            log.warning("could not produce a safe-aligned zip after %d retries", retries)
            break
        time.sleep(0.005)

    return blob
