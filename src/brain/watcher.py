"""fs-event watcher daemon: ingest vault mutations as they land.

Closes the uniform 5-min indexing latency on Linux hosts that lack
macOS's launchd `WatchPaths`. Scheduled `brain-auto-extract.timer`
still runs as a backstop (covers events missed during a daemon
outage), but the typical save-to-searchable latency drops from
≤300 s to ≤1 s.

Design:
  * Linux — exec `inotifywait -m -r --format '%w%f %e' $BRAIN_DIR`
    (inotify-tools); parse its stdout line by line.
  * macOS — exec `fswatch -xn $BRAIN_DIR` if available; otherwise
    skip the watcher (launchd WatchPaths already handles it).
  * Windows / missing tools — log and exit 0. The periodic timer is
    still sufficient; we just don't get sub-second latency.

Routing (one file event → one action):
  * Path under `entities/**/*.md`    → `db.upsert_entity_from_file`
  * Path anywhere else under vault   → `ingest_notes.ingest_one`
    (ingest_one internally filters machine-managed dirs + extensions).
  * `semantic.ensure_built` fires after each successful ingest to
    embed any newly-created fact/entity rows.

Debouncing: events on the same absolute path are coalesced inside a
200 ms window so editors that write-rename-replace (Vim, Obsidian)
don't trigger three reindexes.

Run as `brain watch` (foreground) or via the systemd/launchd unit
installed by `brain watch --install-unit`.
"""

from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import brain.config as config


DEBOUNCE_SEC = 0.2

# inotify events that mean "this file now has interesting content
# on disk" — ignore plain OPEN / ACCESS noise and intermediate writes
# (a CLOSE_WRITE fires when the editor finishes).
_INOTIFY_TRIGGERS = frozenset({
    "CLOSE_WRITE", "MOVED_TO", "CREATE", "MOVED_FROM", "DELETE",
})

# fswatch emits a fixed bitmask string we map to the same set.
_FSWATCH_TRIGGERS = frozenset({
    "Created", "Updated", "Renamed", "Removed", "MovedFrom", "MovedTo",
})


_EXCLUDED_PARTS: frozenset[str] = frozenset({
    "raw", ".git", ".vec", "logs", ".extract.lock.d",
    ".obsidian", ".trash", "_archive", "node_modules",
    ".brain.rdf",
})


def _should_handle(path: Path) -> bool:
    """Cheap pre-filter so we don't even debounce noisy dirs.

    ingest_one / upsert_entity_from_file apply the authoritative
    checks; this one just drops the obviously-uninteresting events
    so the debounce queue stays small.
    """
    if path.suffix.lower() != ".md":
        return False
    if not _is_under_vault(path):
        return False
    try:
        rel_parts = path.resolve().relative_to(config.BRAIN_DIR.resolve()).parts
    except (ValueError, OSError):
        return False
    for part in rel_parts[:-1]:
        if part in _EXCLUDED_PARTS:
            return False
        if part.startswith("."):
            return False
    return True


def _is_under_vault(path: Path) -> bool:
    try:
        path.resolve().relative_to(config.BRAIN_DIR.resolve())
        return True
    except (ValueError, OSError):
        return False


def _is_entity_file(path: Path) -> bool:
    """True when `path` is `<vault>/entities/<type>/*.md`."""
    if not _is_under_vault(path):
        return False
    try:
        rel = path.resolve().relative_to(config.BRAIN_DIR.resolve())
    except (ValueError, OSError):
        return False
    parts = rel.parts
    return len(parts) >= 3 and parts[0] == "entities" and parts[-1].endswith(".md")


def _dispatch(path: Path, verbose: bool = False) -> None:
    """Route one mutation event to the right ingest path + refresh
    watermark + semantic index. Swallows all exceptions so a single
    malformed file can't kill the daemon.
    """
    try:
        from brain import freshness
    except Exception:
        freshness = None  # pragma: no cover — import should never fail

    try:
        if _is_entity_file(path):
            if verbose:
                print(f"  entity: {path}", flush=True)
            if path.exists():
                from brain import db
                db.upsert_entity_from_file(path)
            else:
                from brain import db
                db.delete_entity_by_path(path)
            if freshness:
                try:
                    freshness.bump("entities")
                except Exception:
                    pass
        else:
            if verbose:
                print(f"  note:   {path}", flush=True)
            from brain import ingest_notes
            ingest_notes.ingest_one(path)
            if freshness:
                try:
                    freshness.bump("notes")
                except Exception:
                    pass

        # Embed any new rows that just landed. `ensure_built` is a
        # ~1 ms DB probe when there's nothing new.
        try:
            from brain import semantic
            semantic.ensure_built()
        except Exception:
            pass
    except Exception as exc:
        if verbose:
            print(f"  error:  {path}: {exc}", flush=True)


class _Debouncer:
    """Coalesce rapid duplicate events on the same path.

    Each fs event arms a timer; if another event on the same path
    arrives before the timer fires, we reset the timer. When the
    timer fires we dispatch once. Not a thread-safety masterpiece —
    events are serialized on the watcher thread — but defensive
    against the watcher pumping events faster than dispatch runs.
    """

    def __init__(self, dispatch, delay: float = DEBOUNCE_SEC, verbose: bool = False):
        self._dispatch = dispatch
        self._delay = delay
        self._verbose = verbose
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def arm(self, path: Path) -> None:
        key = str(path)
        with self._lock:
            t = self._timers.pop(key, None)
            if t is not None:
                t.cancel()
            timer = threading.Timer(self._delay, self._fire, args=(key,))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def _fire(self, key: str) -> None:
        with self._lock:
            self._timers.pop(key, None)
        self._dispatch(Path(key), verbose=self._verbose)

    def drain(self) -> None:
        """Fire pending timers immediately; used on shutdown."""
        with self._lock:
            for key, t in list(self._timers.items()):
                t.cancel()
            keys = list(self._timers.keys())
            self._timers.clear()
        for key in keys:
            self._dispatch(Path(key), verbose=self._verbose)


# ---------------------------------------------------------------------------
# Watcher backends
# ---------------------------------------------------------------------------


def _which(*candidates: str) -> str | None:
    for name in candidates:
        p = shutil.which(name)
        if p:
            return p
    return None


def _run_inotifywait(debouncer: _Debouncer, verbose: bool = False) -> int:
    """Stream inotifywait output until SIGINT/SIGTERM. Returns exit code."""
    bin_path = _which("inotifywait")
    if bin_path is None:
        print(
            "brain watch: inotifywait not found. Install `inotify-tools` "
            "(e.g. `sudo apt install inotify-tools` on Debian/Ubuntu).",
            file=sys.stderr,
        )
        return 2

    cmd = [
        bin_path,
        "-m",                   # monitor (don't exit after first event)
        "-r",                   # recursive
        "-q",                   # quiet (no status on stderr)
        "--format", "%w%f\t%e",  # path\tevents
        # Exclude the noisiest machine-managed trees with a single
        # regex — inotifywait's `--exclude` takes one pattern.
        "--exclude",
        r"(^|/)(raw|\.git|\.vec|logs|\.extract\.lock\.d|\.obsidian|"
        r"\.trash|_archive|node_modules|\.brain\.rdf)(/|$)",
        "-e", "close_write",
        "-e", "moved_to",
        "-e", "moved_from",
        "-e", "delete",
        "-e", "create",
        str(config.BRAIN_DIR),
    ]
    if verbose:
        print(f"brain watch: exec {' '.join(cmd)}", flush=True)

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,  # line-buffered
    )
    assert proc.stdout is not None  # for mypy

    stopped = threading.Event()

    def _handle_signal(signum, _frame):
        stopped.set()
        try:
            proc.terminate()
        except Exception:
            pass
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        for raw in proc.stdout:
            if stopped.is_set():
                break
            line = raw.rstrip("\n")
            if not line or "\t" not in line:
                continue
            path_str, events_str = line.split("\t", 1)
            events = set(events_str.replace(",ISDIR", "").split(","))
            if not (events & _INOTIFY_TRIGGERS):
                continue
            path = Path(path_str)
            if not _should_handle(path):
                continue
            debouncer.arm(path)
    finally:
        debouncer.drain()
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return proc.returncode or 0


def _run_fswatch(debouncer: _Debouncer, verbose: bool = False) -> int:
    """macOS fallback. launchd WatchPaths is the primary macOS mechanism;
    this is available so `brain watch` works uniformly across OSes
    when a user runs it in the foreground for debugging."""
    bin_path = _which("fswatch")
    if bin_path is None:
        print(
            "brain watch: fswatch not found. macOS hosts typically rely on "
            "the launchd WatchPaths unit installed by `brain install`. "
            "If you want `brain watch` specifically, `brew install fswatch`.",
            file=sys.stderr,
        )
        return 2

    cmd = [bin_path, "-x", "-r", "--event-flags", str(config.BRAIN_DIR)]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    assert proc.stdout is not None

    stopped = threading.Event()

    def _handle_signal(signum, _frame):
        stopped.set()
        try:
            proc.terminate()
        except Exception:
            pass
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        for raw in proc.stdout:
            if stopped.is_set():
                break
            line = raw.rstrip("\n")
            if not line:
                continue
            # fswatch format: "<path> <flag1> <flag2> ..."
            parts = line.split(" ")
            path = Path(parts[0])
            flags = set(parts[1:])
            if not (flags & _FSWATCH_TRIGGERS):
                continue
            if not _should_handle(path):
                continue
            debouncer.arm(path)
    finally:
        debouncer.drain()
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return proc.returncode or 0


def watch_vault(verbose: bool = False) -> int:
    """Main entry point. Returns an exit code (0 clean shutdown,
    ≥2 backend-missing).
    """
    config.ensure_dirs()
    if verbose:
        print(f"brain watch: vault={config.BRAIN_DIR}", flush=True)

    debouncer = _Debouncer(_dispatch, delay=DEBOUNCE_SEC, verbose=verbose)

    system = platform.system()
    if system == "Linux":
        return _run_inotifywait(debouncer, verbose=verbose)
    if system == "Darwin":
        return _run_fswatch(debouncer, verbose=verbose)

    print(
        f"brain watch: no fs-event backend on {system}. "
        "Relying on scheduled timer for indexing.",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# Systemd unit install
# ---------------------------------------------------------------------------


def _systemd_user_dir() -> Path:
    """`$XDG_CONFIG_HOME/systemd/user` with `~/.config/systemd/user` fallback."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def _template_path() -> Path:
    """Resolve the unit template relative to this file (mirrors the
    existing `templates/systemd/` layout)."""
    here = Path(__file__).resolve()
    # src/brain/watcher.py → repo root has templates/systemd/…
    for candidate in (
        here.parent.parent.parent / "templates" / "systemd"
        / "brain-watcher.service.tmpl",
        Path("/usr/local/share/brain/templates/systemd/brain-watcher.service.tmpl"),
    ):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "brain-watcher.service.tmpl not found — expected under "
        "repo-root/templates/systemd/ or /usr/local/share/brain/."
    )


def install_unit(enable: bool = True) -> int:
    """Render the systemd user unit and (optionally) enable+start it.

    Linux-only. Mac users should rely on the existing launchd
    `WatchPaths` mechanism (`brain install` wires it).
    """
    if platform.system() != "Linux":
        print(
            "brain watch --install-unit: Linux only. On macOS the "
            "launchd WatchPaths unit installed by `brain install` "
            "covers this — no separate watcher needed.",
            file=sys.stderr,
        )
        return 1

    try:
        tmpl = _template_path().read_text()
    except FileNotFoundError as exc:
        print(f"brain watch: {exc}", file=sys.stderr)
        return 2

    brain_dir = str(config.BRAIN_DIR)
    home = str(Path.home())
    # `brain watch` resolves via PATH; fall back to the current
    # interpreter so a venv install still works.
    exec_bin = _which("brain") or f"{sys.executable} -m brain.cli"
    rendered = (tmpl
                .replace("{{BRAIN_DIR}}", brain_dir)
                .replace("{{HOME}}", home)
                .replace("{{BRAIN_CMD}}", exec_bin))

    unit_dir = _systemd_user_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    target = unit_dir / "brain-watcher.service"
    target.write_text(rendered)
    print(f"wrote {target}")

    if not enable:
        return 0

    # Best-effort systemctl calls — don't fail if the user isn't
    # running systemd-as-pid1 (e.g. WSL1, containers).
    for args in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "brain-watcher.service"],
    ):
        try:
            subprocess.run(args, check=False)
        except FileNotFoundError:
            print(
                "systemctl not found — unit written but not activated. "
                "Start it manually once systemd is available.",
                file=sys.stderr,
            )
            return 0
    print("brain-watcher.service enabled; tail logs with "
          "`journalctl --user -u brain-watcher -f`.")
    return 0
