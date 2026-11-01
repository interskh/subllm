from __future__ import annotations

import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from subllm.errors import ClientError


def prepare_out_dir(out_dir: str | Path) -> Path:
    try:
        dest = Path(out_dir).expanduser().resolve()
        dest.mkdir(parents=True, exist_ok=True)
        if not (dest.is_dir() and os.access(dest, os.W_OK | os.X_OK)):
            raise NotADirectoryError(f"not a writable directory: {dest}")
    except (OSError, RuntimeError) as e:
        raise ClientError(f"cannot use out_dir {out_dir}: {e}") from e
    return dest


def _is_image(head: bytes) -> bool:
    return (
        head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a"))
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
    )


def resolve_references(
    references: Sequence[str | Path], *, forbid_comma: bool = False,
) -> list[Path]:
    """Absolute paths of reference images, or ClientError for the first bad one.
    Runs before any subprocess/browser so a bad input never spends quota."""
    refs: list[Path] = []
    for r in references:
        try:
            p = Path(r).expanduser()
            ok = p.is_file() and os.access(p, os.R_OK)
            if ok:
                with open(p, "rb") as fh:
                    head = fh.read(12)
            p = p.resolve()
        except (OSError, RuntimeError) as e:
            raise ClientError(f"cannot use reference image {r}: {e}") from e
        if not ok:
            raise ClientError(f"reference image is not a readable file: {r}")
        if not _is_image(head):
            raise ClientError(f"reference image is not a PNG, JPEG, WebP or GIF file: {r}")
        if forbid_comma and "," in str(p):
            raise ClientError(f"reference image path must not contain ',': {r}")
        refs.append(p)
    return refs


def save_numbered(files: list[tuple[Path, str]], dest: Path) -> list[Path]:
    stem = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{secrets.token_hex(4)}"
    saved: list[Path] = []
    for n, (src, ext) in enumerate(files, 1):
        target = dest / f"{stem}-{n}{ext}"
        try:
            with open(src, "rb") as fin, open(target, "xb") as fout:
                shutil.copyfileobj(fin, fout)
        except OSError as e:
            raise ClientError(f"cannot write image {target}: {e}") from e
        saved.append(target)
    return saved
