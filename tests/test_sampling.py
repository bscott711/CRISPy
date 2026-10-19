"""Tests for the generic fast sampler thread and its read helpers."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from CRISPy.sampling import Sampler, device_present, read_float, read_str


class _Collector:
    def __init__(self, want: int) -> None:
        self.items: list[int] = []
        self.done = threading.Event()
        self._want = want

    def __call__(self, item: int) -> None:
        self.items.append(item)
        if len(self.items) >= self._want:
            self.done.set()


def _counter():
    n = {"i": 0}

    def read() -> int:
        n["i"] += 1
        return n["i"]

    return read


def test_read_helpers_return_none_instead_of_raising() -> None:
    core = MagicMock()
    core.getProperty.side_effect = lambda label, prop: {"a": "1.5", "b": "x"}[prop]
    assert read_float(core, "L", "a") == 1.5
    assert read_float(core, "L", "b") is None  # not a number
    assert read_float(core, "L", "c") is None  # KeyError
    assert read_str(core, "L", "b") == "x"
    assert read_str(core, "L", "c") is None


def test_device_present() -> None:
    core = MagicMock()
    core.getLoadedDevices.return_value = ["L"]
    assert device_present(core, "L")
    assert not device_present(core, "M")
    core.getLoadedDevices.side_effect = RuntimeError("core gone")
    assert device_present(core, "L")  # can't tell: assume it's still there


def test_sampler_delivers_readings_in_order() -> None:
    got = _Collector(5)
    s = Sampler(_counter(), got, max_rate_hz=1000)
    s.start()
    try:
        assert got.done.wait(2.0)
    finally:
        s.stop()
    assert got.items[:5] == [1, 2, 3, 4, 5]
    assert not s.running


def test_sampler_respects_max_rate() -> None:
    got = _Collector(10**9)
    s = Sampler(_counter(), got, max_rate_hz=40)
    s.start()
    got.done.wait(0.5)
    s.stop()
    assert 8 <= len(got.items) <= 30  # ~20 expected


def test_none_readings_are_not_delivered() -> None:
    got = _Collector(1)
    s = Sampler(lambda: None, got)
    s.start()
    try:
        assert not got.done.wait(0.3)
        assert s.running
    finally:
        s.stop()


def test_sampler_ends_when_the_callback_raises() -> None:
    def boom(_: int) -> None:
        raise RuntimeError("gone")

    s = Sampler(_counter(), boom, max_rate_hz=1000)
    s.start()
    s.join(2.0)
    assert not s.running


def test_sampler_ends_when_the_device_is_absent() -> None:
    reads = {"n": 0}

    def read() -> int:
        reads["n"] += 1
        return 1

    s = Sampler(read, lambda _: None, present=lambda: False)
    s.start()
    s.join(2.0)
    assert not s.running
    assert reads["n"] == 0


def test_start_and_stop_are_idempotent_and_restartable() -> None:
    got = _Collector(1)
    s = Sampler(_counter(), got, max_rate_hz=1000)
    s.stop()
    s.start()
    s.start()
    assert got.done.wait(2.0)
    s.stop()
    s.stop()
    seen = len(got.items)
    s.start()
    threading.Event().wait(0.1)
    s.stop()
    assert len(got.items) > seen
