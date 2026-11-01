from __future__ import annotations

import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

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
