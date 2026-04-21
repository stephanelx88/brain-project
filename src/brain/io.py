"""Atomic-write primitives for the brain framework.

Storage-contract clause 3: entity writes, index writes, and derived-artifact
writes must survive interrupt (launchd kill, OS reboot, Ctrl-C). A plain
`Path.write_text(...)` truncates the destination and streams bytes — a crash
mid-write leaves either a zero-byte file or truncated markdown, with no way
to tell apart from a legitimately-empty file. Multiply across ~30 call sites
and that's a blocker-level silent-data-loss surface.

Fix: write-to-temp + `os.replace()`. `os.replace` is atomic on POSIX and
Windows as long as the temp file lives on the same filesystem as the target
(we put it next to the target to guarantee that). The temp name embeds the
pid so two processes renaming into the same final path never step on each
other's intermediate file.

Public API:
    atomic_write_text(path, text)
    atomic_write_bytes(path, data)

Both auto-create parent directories. Neither swallows permission errors.
"""

from __future__ import annotations

import os
from pathlib import Path


def _tmp_path(path: Path) -> Path:
    """Per-pid sibling temp path so concurrent writers don't collide.

    Uses the full final name + a `.tmp.<pid>` suffix (NOT
    `with_suffix(...)`, which would clobber an existing suffix like
    `.md` → `.tmp.1234`). Keeping the original name intact makes it
    obvious in `ls` which file a stranded temp belonged to.
    """
    return path.parent / f"{path.name}.tmp.{os.getpid()}"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically.

    Semantics: write to a sibling temp file, `fsync` its contents, then
    `os.replace` over the destination. The replace is atomic on POSIX
    and Windows; a crash before the replace leaves only the temp file,
    which the next successful write will overwrite. Auto-creates
    `path.parent`. Re-raises on any error after cleaning up the temp.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        # os.open + os.fdopen keeps the fd in our hands so we can fsync
        # before close — ordinary open() doesn't expose the fd the same
        # way. O_WRONLY|O_CREAT|O_TRUNC matches write-mode semantics.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            # fdopen took ownership of the fd; its __exit__ closes it.
            # Nothing extra to do here except fall through to tmp cleanup.
            raise
        os.replace(tmp, path)
    except BaseException:
        # Best-effort cleanup — don't mask the original exception with a
        # secondary FileNotFoundError if the tmp was never created.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write `text` to `path` atomically. See atomic_write_bytes for the
    contract. Encoding defaults to UTF-8 (matches Path.write_text default
    on all supported Pythons)."""
    atomic_write_bytes(Path(path), text.encode(encoding))
