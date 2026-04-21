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
