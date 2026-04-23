"""Tests for brain.resource_guard."""
import pytest
from brain.resource_guard import clearance_level


def _level(cpu, idle, ac=True, screen=0, mem=0):
    return clearance_level(
        cpu=cpu, mem=mem, session_idle=idle, on_ac=ac, screen_idle=screen
    )


class TestClearanceLevel:
    def test_level_0_high_cpu(self):
        assert _level(cpu=75, idle=0) == 0

    def test_level_0_cpu_at_boundary(self):
        # exactly at threshold is NOT below it
        assert _level(cpu=60, idle=0) == 0

    def test_level_1_cpu_just_below_threshold(self):
        assert _level(cpu=59.9, idle=0) == 1

    def test_level_1_cpu_very_low_but_no_idle(self):
        # CPU low but session not idle enough for L2
        assert _level(cpu=10, idle=0) == 1

    def test_level_2_needs_idle(self):
        assert _level(cpu=35, idle=60) == 2

    def test_level_2_insufficient_idle(self):
        assert _level(cpu=35, idle=59) == 1

    def test_level_2_cpu_too_high(self):
        assert _level(cpu=41, idle=120) == 1

    def test_level_3_needs_ac(self):
        assert _level(cpu=15, idle=180, ac=False) == 2

    def test_level_3_on_ac(self):
        assert _level(cpu=15, idle=180, ac=True) == 3

    def test_level_3_insufficient_idle(self):
        assert _level(cpu=15, idle=179, ac=True) == 2

    def test_level_4_needs_screen_idle(self):
        assert _level(cpu=10, idle=300, ac=True, screen=0) == 3

    def test_level_4_screen_idle(self):
        assert _level(cpu=10, idle=300, ac=True, screen=120) == 4

    def test_level_4_screen_just_below(self):
        assert _level(cpu=10, idle=300, ac=True, screen=119) == 3

    def test_level_4_cpu_too_high(self):
        assert _level(cpu=16, idle=300, ac=True, screen=300) == 3

    def test_level_4_no_ac(self):
        assert _level(cpu=10, idle=300, ac=False, screen=300) == 2

    def test_full_idle_machine(self):
        # typical screensaver + idle laptop on charger
        assert _level(cpu=2, idle=600, ac=True, screen=600) == 4


class TestMemoryGate:
    def test_level_0_when_memory_saturated(self):
        # RAM > 90% blocks even the lowest level despite idle CPU
        assert _level(cpu=5, idle=600, ac=True, screen=600, mem=95) == 0

    def test_level_1_mem_at_boundary(self):
        # exactly at L1 threshold is NOT below it
        assert _level(cpu=5, idle=0, mem=90) == 0

    def test_level_1_mem_just_below(self):
        assert _level(cpu=5, idle=0, mem=89.9) == 1

    def test_level_2_blocked_by_mem(self):
        # CPU + idle would allow L2 but mem pressure caps at L1
        assert _level(cpu=5, idle=120, ac=True, mem=85) == 1

    def test_level_3_blocked_by_mem(self):
        assert _level(cpu=5, idle=300, ac=True, mem=75) == 2

    def test_level_4_blocked_by_mem(self):
        assert _level(cpu=5, idle=600, ac=True, screen=600, mem=65) == 3

    def test_level_4_mem_headroom(self):
        assert _level(cpu=5, idle=600, ac=True, screen=600, mem=50) == 4


class TestCrossPlatformProbes:
    """Locks in the Linux fallbacks so resource_guard works on Ubuntu.

    pmset / ioreg exist only on macOS. Previously the catch-all returned
    safe defaults but the Linux signal was always the same — we now
    actually probe sysfs for AC state and skip HID tracking on headless
    servers (where it's unanswerable by design).
    """

    def test_on_ac_linux_reads_sysfs_mains_online(self, tmp_path, monkeypatch):
        from brain import resource_guard as rg
        # Build a fake /sys/class/power_supply/ layout and point the
        # probe at it via monkeypatching Path (scope of the probe is
        # narrow so a targeted patch is cleaner than a full fs fake).
        ps = tmp_path / "power_supply"
        (ps / "AC").mkdir(parents=True)
        (ps / "AC" / "type").write_text("Mains\n")
        (ps / "AC" / "online").write_text("1\n")
        monkeypatch.setattr(rg, "_SYSTEM", "Linux")

        # The probe reads a hardcoded path; indirect via a small shim.
        original = rg.Path
        def fake_path(arg):
            if arg == "/sys/class/power_supply":
                return ps
            return original(arg)
        monkeypatch.setattr(rg, "Path", fake_path)

        assert rg._on_ac_power() is True

    def test_on_ac_linux_reports_false_when_unplugged(self, tmp_path, monkeypatch):
        from brain import resource_guard as rg
        ps = tmp_path / "power_supply"
        (ps / "AC").mkdir(parents=True)
        (ps / "AC" / "type").write_text("Mains\n")
        (ps / "AC" / "online").write_text("0\n")
        monkeypatch.setattr(rg, "_SYSTEM", "Linux")

        original = rg.Path
        def fake_path(arg):
            if arg == "/sys/class/power_supply":
                return ps
            return original(arg)
        monkeypatch.setattr(rg, "Path", fake_path)

        assert rg._on_ac_power() is False

    def test_on_ac_linux_no_sysfs_assumes_ac(self, tmp_path, monkeypatch):
        """Servers / containers without /sys/class/power_supply must not
        block background jobs — they're effectively always on mains."""
        from brain import resource_guard as rg
        monkeypatch.setattr(rg, "_SYSTEM", "Linux")

        original = rg.Path
        def fake_path(arg):
            if arg == "/sys/class/power_supply":
                return tmp_path / "does-not-exist"
            return original(arg)
        monkeypatch.setattr(rg, "Path", fake_path)

        assert rg._on_ac_power() is True

    def test_on_ac_linux_battery_only_machine_assumes_ac(
        self, tmp_path, monkeypatch
    ):
        """Device with a battery entry but no Mains adapter — unclear
        whether charger is connected. Fail open (True) so background
        jobs still run; a false positive here is better than stalling
        extraction on a laptop the probe can't fully introspect."""
        from brain import resource_guard as rg
        ps = tmp_path / "power_supply"
        (ps / "BAT0").mkdir(parents=True)
        (ps / "BAT0" / "type").write_text("Battery\n")
        monkeypatch.setattr(rg, "_SYSTEM", "Linux")

        original = rg.Path
        def fake_path(arg):
            if arg == "/sys/class/power_supply":
                return ps
            return original(arg)
        monkeypatch.setattr(rg, "Path", fake_path)

        assert rg._on_ac_power() is True

    def test_screen_idle_linux_headless_returns_huge(self, monkeypatch):
        """Headless server → no DISPLAY / WAYLAND_DISPLAY. The guard
        should treat screen-idle thresholds as always cleared so
        background jobs aren't forever gated on a nonexistent user."""
        from brain import resource_guard as rg
        monkeypatch.setattr(rg, "_SYSTEM", "Linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert rg._screen_idle_seconds() >= 1e8

    def test_screen_idle_linux_with_display_but_no_xprintidle_returns_zero(
        self, monkeypatch
    ):
        """Desktop Linux with X11 but no xprintidle tool installed — we
        can't measure idle, so fall back to 0.0 (conservative: never
        elevates the clearance level on unverifiable signals)."""
        from brain import resource_guard as rg
        monkeypatch.setattr(rg, "_SYSTEM", "Linux")
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

        def boom(*a, **kw):
            raise FileNotFoundError("xprintidle not on PATH")
        monkeypatch.setattr(rg.subprocess, "check_output", boom)
        monkeypatch.delenv("XDG_SESSION_ID", raising=False)

        assert rg._screen_idle_seconds() == 0.0

    def test_on_ac_unknown_platform_returns_true(self, monkeypatch):
        """Windows / unknown BSDs: no probe available → safe default of
        True so jobs keep running."""
        from brain import resource_guard as rg
        monkeypatch.setattr(rg, "_SYSTEM", "Windows")
        assert rg._on_ac_power() is True
