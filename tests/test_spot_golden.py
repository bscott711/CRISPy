"""Golden test of the spot edge lock's deterministic functions.

``tests/spot_golden/golden.json`` holds outputs recorded from the LEGACY code
(pymmcore-gui ``asi_z_stack``, a35df08) for stored inputs; this test replays
every case against an implementation and compares.

- Default: the implementation is ``CRISPy.spot``. It must import: a packaging
  slip fails the module, it is never skipped.
- ``SPOT_GOLDEN_LEGACY_DIR=<legacy asi_z_stack dir>`` loads the legacy files
  from that directory instead (proves the golden is self-consistent).
- ``SPOT_GOLDEN_EXACT=1`` compares floats bit for bit; otherwise rtol = atol =
  1e-12, nan equal to nan.

Regenerate with ``tests/spot_golden/make_golden.py`` (legacy code only).
Qt-free: numpy, stdlib and pytest.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import struct
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

GOLDEN_DIR = Path(__file__).parent / "spot_golden"


def _load_make_golden() -> Any:
    spec = importlib.util.spec_from_file_location(
        "spot_golden_make_golden", GOLDEN_DIR / "make_golden.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


MG = _load_make_golden()
GOLDEN = json.loads((GOLDEN_DIR / "golden.json").read_text())
CASES: list[dict[str, Any]] = GOLDEN["cases"]
EXACT = os.environ.get("SPOT_GOLDEN_EXACT") == "1"
RTOL = ATOL = 1e-12


@pytest.fixture(scope="module")
def arrays() -> dict[str, np.ndarray]:
    with np.load(GOLDEN_DIR / "inputs.npz") as z:
        return {k: z[k] for k in z.files}


@pytest.fixture(scope="module")
def impl() -> Any:
    legacy = os.environ.get("SPOT_GOLDEN_LEGACY_DIR")
    if legacy:
        return MG.load_legacy(legacy)
    return MG.load_crispy()


def _bits(x: float) -> bytes:
    return struct.pack("<d", x)


def _same_float(a: float, e: float) -> bool:
    if math.isnan(e) or math.isnan(a):
        return math.isnan(e) and math.isnan(a)
    if EXACT:
        return _bits(a) == _bits(e)
    return math.isclose(a, e, rel_tol=RTOL, abs_tol=ATOL)


def _diff(a: Any, e: Any, path: str = "") -> str | None:
    """First difference between actual *a* and expected *e*, or None."""
    if isinstance(e, bool) or e is None or isinstance(e, (str, int)):
        # bool / int / str / None exactly (a bool is not an int here)
        return None if type(a) is type(e) and a == e else f"{path}: {a!r} != {e!r}"
    if isinstance(e, float):
        if not isinstance(a, float):
            return f"{path}: {a!r} is not a float (expected {e!r})"
        return None if _same_float(a, e) else f"{path}: {a!r} != {e!r}"
    if isinstance(e, list):
        if not isinstance(a, list) or len(a) != len(e):
            return f"{path}: length/type {a!r:.80} != {e!r:.80}"
        for i, (x, y) in enumerate(zip(a, e, strict=True)):
            d = _diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    if isinstance(e, dict):
        if not isinstance(a, dict) or set(a) != set(e):
            return f"{path}: keys {sorted(a) if isinstance(a, dict) else a!r} != {sorted(e)}"
        for k in e:
            d = _diff(a[k], e[k], f"{path}.{k}")
            if d:
                return d
        return None
    return f"{path}: unsupported golden type {type(e)}"  # pragma: no cover


def test_golden_file_is_sane() -> None:
    fns = {c["fn"] for c in CASES}
    assert len(CASES) >= 200
    assert len({c["id"] for c in CASES}) == len(CASES)
    assert fns == set(MG.FUNCS)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_case(case: dict[str, Any], impl: Any, arrays: dict[str, Any]) -> None:
    actual = MG.run_case(impl, case, arrays)
    d = _diff(actual, case["expected"])
    assert d is None, d
