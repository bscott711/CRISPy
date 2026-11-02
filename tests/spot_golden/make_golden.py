"""Golden outputs of the spot edge lock's deterministic functions.

Run once against the LEGACY code (pymmcore-gui ``asi_z_stack``) to write
``golden.json`` and ``inputs.npz``::

    uv run python tests/spot_golden/make_golden.py --legacy-dir <.../asi_z_stack>

``tests/test_spot_golden.py`` replays every case in ``golden.json`` against an
implementation (``CRISPy.spot`` or a legacy dir) with :func:`run_case` and
compares with the stored ``expected``. Frames and sweeps are stored (in
``inputs.npz`` / the case args), so nothing depends on a random number stream.

Qt-free: numpy and the stdlib only.
"""

from __future__ import annotations

import argparse
import atexit
import importlib
import json
import math
import shutil
import sys
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
GOLDEN_JSON = HERE / "golden.json"
INPUTS_NPZ = HERE / "inputs.npz"

#: The legacy files copied into the throwaway package ``legacy_spot``.
LEGACY_FILES = (
    "spot_focus.py",
    "spot_focus_worker.py",
    "spot_synthetic.py",
    "spot_frame_shm.py",
    "toupcam_source.py",
)
_LEGACY_IMPORT = "from pymmcore_gui.asi_z_stack.spot_synthetic import"


# ---------------------------------------------------------------------------
# Loading an implementation
# ---------------------------------------------------------------------------


def _bundle(pkg: str) -> SimpleNamespace:
    imp = importlib.import_module
    return SimpleNamespace(
        spot_focus=imp(f"{pkg}.spot_focus"),
        worker=imp(f"{pkg}.spot_focus_worker"),
        synthetic=imp(f"{pkg}.spot_synthetic"),
    )


def load_legacy(legacy_dir: str | Path) -> SimpleNamespace:
    """Load the legacy spot modules from *legacy_dir* as package ``legacy_spot``.

    The files are copied into a temp dir (they use relative imports; the one
    absolute ``pymmcore_gui`` import is rewritten in the copy).
    """
    if "legacy_spot" in sys.modules and getattr(
        sys.modules["legacy_spot"], "_src", None
    ) == str(legacy_dir):
        return _bundle("legacy_spot")
    src = Path(legacy_dir)
    tmp = Path(tempfile.mkdtemp(prefix="legacy_spot_"))
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    pkg = tmp / "legacy_spot"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(f"_src = {str(legacy_dir)!r}\n")
    for name in LEGACY_FILES:
        text = (src / name).read_text()
        text = text.replace(_LEGACY_IMPORT, "from .spot_synthetic import")
        (pkg / name).write_text(text)
    sys.path.insert(0, str(tmp))
    return _bundle("legacy_spot")


def load_crispy() -> SimpleNamespace:
    """Load ``CRISPy.spot`` (raises ImportError when it is not there yet)."""
    return _bundle("CRISPy.spot")


# ---------------------------------------------------------------------------
# JSON encoding of results (identical for expected and actual)
# ---------------------------------------------------------------------------


def enc(x: Any) -> Any:
    """Turn a result into plain JSON types (nan kept as a float nan)."""
    if is_dataclass(x) and not isinstance(x, type):
        return enc(asdict(x))
    if isinstance(x, dict):
        return {str(k): enc(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [enc(v) for v in x]
    if isinstance(x, np.ndarray):
        return enc(x.tolist())
    if isinstance(x, np.generic):
        return x.item()
    return x


# ---------------------------------------------------------------------------
# Case runner
# ---------------------------------------------------------------------------


def _frame(arrays: Any, spec: dict[str, Any]) -> np.ndarray:
    a = np.asarray(arrays[spec["key"]])
    if spec.get("tile_rows"):
        a = np.tile(a, (int(spec["tile_rows"]), 1))
    if spec.get("crop"):
        y0, x0 = spec["crop"]
        a = a[y0:, x0:]
    return a


def _fix(m: SimpleNamespace, d: dict[str, Any]) -> Any:
    return m.spot_focus.EdgeFix(**d)


def _ecal(m: SimpleNamespace, d: dict[str, Any]) -> Any:
    return m.spot_focus.EdgeCalibration.from_dict(d)


def _fcal(m: SimpleNamespace, d: dict[str, Any]) -> Any:
    return m.spot_focus.FocusCalibration.from_dict(d)


def _params(m: SimpleNamespace, d: dict[str, Any] | None) -> Any:
    return m.spot_focus.SpotLoopParams.from_dict(d or {})


def _kw(kw: dict[str, Any]) -> dict[str, Any]:
    out = dict(kw)
    if "roi_offset" in out:
        out["roi_offset"] = tuple(out["roi_offset"])
    return out


def _cal_dict(cal: Any) -> dict[str, Any]:
    d = cal.to_dict()
    d.pop("created", None)
    for e in d.get("edges", []):
        e.pop("created", None)
    return d


def _fn_measure_edge(m: SimpleNamespace, a: dict[str, Any], arrays: Any) -> Any:
    frame = _frame(arrays, a["frame"])
    return m.spot_focus.measure_edge(frame, a["box"], **_kw(a.get("kw", {})))


def _fn_snap_box(m: SimpleNamespace, a: dict[str, Any], arrays: Any) -> Any:
    return m.spot_focus.snap_box(a["box"], **a.get("kw", {}))


def _fn_edge_error(m: SimpleNamespace, a: dict[str, Any], arrays: Any) -> Any:
    return m.spot_focus.edge_error(
        _fix(m, a["fix"]), _ecal(m, a["cal"]), a.get("offset_um", 0.0)
    )


def _fn_check_fix(m: SimpleNamespace, a: dict[str, Any], arrays: Any) -> Any:
    return m.spot_focus.check_fix(
        _fix(m, a["fix"]), _ecal(m, a["cal"]), _params(m, a.get("params"))
    )


def _fn_edge_noise_um(m: SimpleNamespace, a: dict[str, Any], arrays: Any) -> Any:
    return m.spot_focus.edge_noise_um(_ecal(m, a["cal"]))


def _fn_detector_mismatch(m: SimpleNamespace, a: dict[str, Any], arrays: Any) -> Any:
    args = [a["detector"]] + ([a["sigma_px"]] if "sigma_px" in a else [])
    return m.spot_focus.detector_mismatch(_fcal(m, a["cal"]), *args)


def _fn_fit_edge_calibration(m: SimpleNamespace, a: dict[str, Any], arrays: Any) -> Any:
    kw = dict(a.get("kw", {}))
    kw["box"] = tuple(kw["box"])
    cal = m.spot_focus.fit_edge_calibration(a["p_um"], a["edge_px"], **kw)
    d = _cal_dict(cal)
    assert cal.created, "created must be filled"
    return {
        "cal": d,
        "equal_after_roundtrip": m.spot_focus.EdgeCalibration.from_dict(
            cal.to_dict()
        ).to_dict()
        == cal.to_dict(),
    }


def _fn_edgecal_roundtrip(m: SimpleNamespace, a: dict[str, Any], arrays: Any) -> Any:
    cal = _ecal(m, a["cal"])
    return {"cal": cal.to_dict(), "again": _ecal(m, cal.to_dict()).to_dict()}


def _fn_rezero(m: SimpleNamespace, a: dict[str, Any], arrays: Any) -> Any:
    cal = _ecal(m, a["cal"])
    return m.spot_focus.rezero(cal, e0_px=a["e0_px"], p0_um=a["p0_um"]).to_dict()


def _fn_load_calibration(m: SimpleNamespace, a: dict[str, Any], arrays: Any) -> Any:
    cal, msg = m.spot_focus.load_calibration(a["d"])
    return [None if cal is None else cal.to_dict(), msg]


def _fn_controller(m: SimpleNamespace, a: dict[str, Any], arrays: Any) -> Any:
    ctl = m.spot_focus.SpotFocusController(
        _params(m, a.get("params")),
        a["p_start_um"],
        linear_range_um=a.get("linear_range_um"),
    )
    outs: list[Any] = []
    for op in a["ops"]:
        if op[0] == "update":
            outs.append(ctl.update(op[1], op[2]))
        elif op[0] == "bad":
            outs.append(ctl.bad(op[1]))
        elif op[0] == "good":
            outs.append(ctl.good())
        else:  # pragma: no cover
            raise ValueError(op)
    return {
        "outs": outs,
        "p_cmd_um": ctl.p_cmd_um,
        "fault": ctl.fault,
        "n_moves": ctl.n_moves,
    }


def _fn_settle_time_s(m: SimpleNamespace, a: dict[str, Any], arrays: Any) -> Any:
    return m.worker.settle_time_s(a["exposure_ms"], a["fps"])


def _fn_settle(m: SimpleNamespace, a: dict[str, Any], arrays: Any) -> Any:
    s = m.worker.Settle()
    outs: list[Any] = [[s.seen, s.until]]
    for op in a["ops"]:
        if op[0] == "arm":
            outs.append(s.arm(op[1], op[2], op[3]))
        elif op[0] == "frame":
            outs.append(s.frame(op[1]))
        elif op[0] == "remaining":
            outs.append(s.remaining_s(op[1]))
        else:  # pragma: no cover
            raise ValueError(op)
    return {"outs": outs, "seen": s.seen, "until": s.until}


def _fn_defaults(m: SimpleNamespace, a: dict[str, Any], arrays: Any) -> Any:
    sf, w = m.spot_focus, m.worker
    cal = sf.EdgeCalibration(
        box=(0, 0, 32, 32),
        axis="x",
        polarity=1,
        slope_px_per_um=1.5,
        e0_px=10.0,
        p0_um=1.0,
    )
    return {
        "loop_params": sf.SpotLoopParams().to_dict(),
        "loop_params_from_dict": sf.SpotLoopParams.from_dict(
            {"kp": 1.25, "bogus": 3, "p_min_um": 0.0}
        ).to_dict(),
        "edge_cal_defaults": cal.to_dict(),
        "constants": {
            "EDGE_DETECTORS": sf.EDGE_DETECTORS,
            "EDGE_DETECTOR_LABELS": sf.EDGE_DETECTOR_LABELS,
            "DEFAULT_DETECTOR": sf.DEFAULT_DETECTOR,
            "DEFAULT_SIGMA_PX": sf.DEFAULT_SIGMA_PX,
            "SIGMA_PX_RANGE": sf.SIGMA_PX_RANGE,
        },
        "worker_constants": {
            "SETTLE_MIN_FRAMES": w.SETTLE_MIN_FRAMES,
            "SETTLE_MIN_S": w.SETTLE_MIN_S,
            "MOVE_ACK_TIMEOUT_S": w.MOVE_ACK_TIMEOUT_S,
            "NO_FRAME_TIMEOUT_S": w.NO_FRAME_TIMEOUT_S,
            "TELEMETRY_PERIOD_S": w.TELEMETRY_PERIOD_S,
            "PREVIEW_FIT_PERIOD_S": w.PREVIEW_FIT_PERIOD_S,
            "SPOT_MAX_AGE_S": w.SPOT_MAX_AGE_S,
            "FRAME_WAIT_S": w.FRAME_WAIT_S,
            "CHANNELS": w.CHANNELS,
        },
    }


FUNCS = {
    "measure_edge": _fn_measure_edge,
    "snap_box": _fn_snap_box,
    "edge_error": _fn_edge_error,
    "check_fix": _fn_check_fix,
    "edge_noise_um": _fn_edge_noise_um,
    "detector_mismatch": _fn_detector_mismatch,
    "fit_edge_calibration": _fn_fit_edge_calibration,
    "edgecal_roundtrip": _fn_edgecal_roundtrip,
    "rezero": _fn_rezero,
    "load_calibration": _fn_load_calibration,
    "controller": _fn_controller,
    "settle_time_s": _fn_settle_time_s,
    "settle": _fn_settle,
    "defaults": _fn_defaults,
}


def run_case(m: SimpleNamespace, case: dict[str, Any], arrays: Any) -> Any:
    """Run one case on implementation *m*; return the encoded result.

    An exception becomes ``{"raises": <type name>, "message": <str>}``.
    """
    try:
        return enc(FUNCS[case["fn"]](m, case["args"], arrays))
    except Exception as exc:  # noqa: BLE001
        return {"raises": type(exc).__name__, "message": str(exc)}


# ---------------------------------------------------------------------------
# Case definitions (generation only)
# ---------------------------------------------------------------------------

NAN = math.nan


def _fixd(position: float, axis: str = "x", **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "position": position,
        "x": position if axis == "x" else 100.0,
        "y": 100.0 if axis == "x" else position,
        "axis": axis,
        "polarity": 1,
        "contrast": 100.0,
        "low": 1.0,
        "high": 101.0,
        "angle_deg": 0.0,
        "residual_px": 0.1,
        "valid_fraction": 1.0,
        "saturated_frac": 0.0,
        "peak": 120.0,
        "ok": True,
        "reason": "",
        "detector": "gaussderiv",
        "sigma_px": 6.0,
        "edge_width_px": 12.0,
    }
    d.update(over)
    return d


def _ecald(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "kind": "edge",
        "box": [38, 60, 48, 48],
        "axis": "x",
        "polarity": 1,
        "slope_px_per_um": 2.0,
        "e0_px": 100.0,
        "p0_um": 10.0,
        "r2": 0.995,
        "residual_px": 0.04,
        "linear_range_um": 1.0,
        "noise_px": 0.05,
        "angle_deg": 0.0,
        "contrast": 100.0,
        "n_points": 9,
        "sweep": [[9.0, 98.0], [10.0, 100.0], [11.0, 102.0]],
        "warnings": ["a note"],
        "record_path": "",
        "created": "2026-10-28T12:00:00",
        "detector": "gaussderiv",
        "detector_sigma_px": 6.0,
        "edge_width_px": 13.5,
        "box_normal_px": 48,
    }
    d.update(over)
    return d


def _ecal_b(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "box": [86, 15, 48, 48],
        "axis": "y",
        "slope_px_per_um": -1.8,
        "e0_px": 50.0,
        "noise_px": 0.03,
        "residual_px": 0.05,
        "edge_width_px": None,
    }
    d.update(over)
    return _ecald(**d)


def _fcald(*edges: dict[str, Any], **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "kind": "edges",
        "edges": list(edges),
        "p0_um": 10.0,
        "created": "2026-10-28T12:00:00",
        "record_path": "x.npz",
        "warnings": ["w1"],
    }
    d.update(over)
    return d


def _render_frames(syn: Any) -> dict[str, np.ndarray]:
    """Render the stored frames once (the only place an RNG is used)."""
    rng = np.random.default_rng(20261029)
    shape = (170, 240)
    f = {}
    f["f1"] = syn.add_noise(
        syn.render_led_patch(shape, (110, 85), level=100, background=1, seed=1),
        noise=1.5,
        rng=rng,
        bits=8,
    )
    f["f2"] = syn.add_noise(
        syn.render_led_patch(
            shape, (112.3, 84.6), angle_deg=2.0, level=1500, background=20, seed=2
        ),
        noise=4.0,
        rng=rng,
        hot_pixels=3,
        bits=12,
    )
    f["f3_sat"] = syn.add_noise(
        syn.render_led_patch(shape, (110, 85), level=400, background=1, seed=3),
        noise=1.0,
        rng=rng,
        bits=8,
    )
    f["f4_low"] = syn.add_noise(
        syn.render_led_patch(shape, (110, 85), level=3, background=1, seed=4),
        noise=0.5,
        rng=rng,
        bits=8,
    )
    f["f5_soft"] = syn.add_noise(
        syn.render_led_patch(
            shape,
            (110, 85),
            edge_sigma=6.0,
            level=800,
            background=10,
            ghost=0.5,
            seed=5,
        ),
        noise=2.0,
        rng=rng,
        bits=10,
    )
    # f6: f1 with blocks of rows of weaker edge response (exercises the
    # per-row peak threshold of the Gaussian-derivative detector)
    dim = f["f1"].astype(np.float64)
    gain = [1.0, 0.9, 1.0, 0.5, 1.0, 0.35, 1.0, 0.27, 1.0, 0.22, 1.0, 0.15]
    for k, g in enumerate(gain):
        r0 = 50 + 6 * k
        dim[r0 : r0 + 6, :] = 1.0 + g * (dim[r0 : r0 + 6, :] - 1.0)
    f["f6_dimrows"] = np.rint(dim).astype(np.uint8)
    return f


def _phi(z: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.vectorize(math.erf)(z / math.sqrt(2.0)))


def _asym_profile(e: float = 100.0, n: int = 400) -> np.ndarray:
    x = np.arange(n, dtype=np.float64)
    return 2000.0 * (0.75 * _phi((x - e) / 5.0) + 0.25 * _phi((x - e - 22.0) / 9.0))


def _asym_box(width: int, e: float = 100.0) -> list[int]:
    bx = int(e) - width // 2
    return [bx - bx % 2, 60, width, 60]


def _boxes(cx: float, cy: float, bw: int = 48, bh: int = 48) -> dict[str, list[Any]]:
    """Left / right / top / bottom edge boxes of the default patch (96 x 92)."""
    return {
        "left": [cx - 48 - bw / 2, cy - bh / 2, bw, bh],
        "right": [cx + 48 - bw / 2, cy - bh / 2, bw, bh],
        "top": [cx - bw / 2, cy - 46 - bh / 2, bw, bh],
        "bottom": [cx - bw / 2, cy + 46 - bh / 2, bw, bh],
    }


def build_cases(
    syn: Any, sweep_measure: Any
) -> tuple[list[dict[str, Any]], dict[str, np.ndarray]]:
    arrays = _render_frames(syn)
    arrays["asym_profile"] = _asym_profile()
    cases: list[dict[str, Any]] = []

    def add(fn: str, cid: str, args: dict[str, Any]) -> None:
        cases.append({"id": f"{fn}:{cid}", "fn": fn, "args": args})

    # -- measure_edge: analytic asymmetric edge ------------------------------
    asym = {"key": "asym_profile", "tile_rows": 200}
    for det in ("gaussderiv", "halfmax"):
        for w in (48, 64, 100, 200):
            add(
                "measure_edge",
                f"asym-w{w}-{det}",
                {"frame": asym, "box": _asym_box(w), "kw": {"detector": det}},
            )
    for s in (3.0, 9.0):
        add(
            "measure_edge",
            f"asym-w48-gaussderiv-s{s:g}",
            {
                "frame": asym,
                "box": _asym_box(48),
                "kw": {"detector": "gaussderiv", "sigma_px": s},
            },
        )

    # -- measure_edge: rendered frames ---------------------------------------
    pol = {"left": ("x", 1), "right": ("x", -1), "top": ("y", 1), "bottom": ("y", -1)}
    b1 = _boxes(110, 85)
    for side, box in b1.items():
        for det in ("gaussderiv", "halfmax"):
            for ch in ("sum", "red"):
                sigmas = (3.0, 6.0, 9.0) if det == "gaussderiv" else (6.0,)
                for s in sigmas:
                    add(
                        "measure_edge",
                        f"f1-{side}-{det}-{ch}-s{s:g}",
                        {
                            "frame": {"key": "f1"},
                            "box": box,
                            "kw": {"detector": det, "channel": ch, "sigma_px": s},
                        },
                    )
            ax, pl = pol[side]
            add(
                "measure_edge",
                f"f1-{side}-{det}-explicit",
                {
                    "frame": {"key": "f1"},
                    "box": box,
                    "kw": {"detector": det, "axis": ax, "polarity": pl},
                },
            )
            add(
                "measure_edge",
                f"f1-{side}-{det}-wrongpol",
                {
                    "frame": {"key": "f1"},
                    "box": box,
                    "kw": {"detector": det, "axis": ax, "polarity": -pl},
                },
            )
    b2 = _boxes(112.3, 84.6, bw=52, bh=50)  # float boxes: snapped inside
    for side, box in b2.items():
        for det in ("gaussderiv", "halfmax"):
            for ch, bay in (
                ("sum", "RGGB"),
                ("red", "RGGB"),
                ("red", "BGGR"),
                ("red", "GRBG"),
                ("red", "GBRG"),
            ):
                add(
                    "measure_edge",
                    f"f2-{side}-{det}-{ch}-{bay}",
                    {
                        "frame": {"key": "f2"},
                        "box": box,
                        "kw": {"detector": det, "channel": ch, "bayer": bay},
                    },
                )
    b5 = _boxes(110, 85, bw=72, bh=64)
    for side, box in b5.items():
        for det in ("gaussderiv", "halfmax"):
            add(
                "measure_edge",
                f"f5-{side}-{det}",
                {"frame": {"key": "f5_soft"}, "box": box, "kw": {"detector": det}},
            )
    for det in ("gaussderiv", "halfmax"):
        add(
            "measure_edge",
            f"f3-saturated-{det}",
            {"frame": {"key": "f3_sat"}, "box": b1["left"], "kw": {"detector": det}},
        )
        add(
            "measure_edge",
            f"f3-saturation-tolerated-{det}",
            {
                "frame": {"key": "f3_sat"},
                "box": b1["left"],
                "kw": {"detector": det, "max_saturated_frac": 0.6},
            },
        )
        add(
            "measure_edge",
            f"f4-lowcontrast-{det}",
            {"frame": {"key": "f4_low"}, "box": b1["left"], "kw": {"detector": det}},
        )
        add(
            "measure_edge",
            f"f4-lowcontrast-allowed-{det}",
            {
                "frame": {"key": "f4_low"},
                "box": b1["left"],
                "kw": {"detector": det, "min_contrast": 1.0},
            },
        )
        add(
            "measure_edge",
            f"f1-explicit-saturation-{det}",
            {
                "frame": {"key": "f1"},
                "box": b1["right"],
                "kw": {"detector": det, "saturation": 60.0},
            },
        )
        add(
            "measure_edge",
            f"f1-min-valid-fraction-{det}",
            {
                "frame": {"key": "f1"},
                "box": b1["top"],
                "kw": {"detector": det, "min_valid_fraction": 0.99},
            },
        )
        add(
            "measure_edge",
            f"f1-edge-outside-box-{det}",
            {"frame": {"key": "f1"}, "box": [150, 61, 48, 48], "kw": {"detector": det}},
        )
        # sensor ROI offset: the frame is f1 cropped from sensor (20, 10) on, the
        # boxes stay in sensor coordinates (so they must match the full f1 fixes)
        for side, box in b1.items():
            add(
                "measure_edge",
                f"f1-roi20x10-{side}-{det}",
                {
                    "frame": {"key": "f1", "crop": [10, 20]},
                    "box": box,
                    "kw": {"detector": det, "roi_offset": [20, 10]},
                },
            )
        add(
            "measure_edge",
            f"f1-roi60x30-outside-{det}",
            {
                "frame": {"key": "f1", "crop": [30, 60]},
                "box": b1["left"],
                "kw": {"detector": det, "roi_offset": [60, 30]},
            },
        )
    tall = [62 - 24, 50, 48, 72]  # left edge, rows 50..122 (12 blocks of 6 rows)
    for det in ("gaussderiv", "halfmax"):
        for s in (3.0, 6.0, 9.0):
            add(
                "measure_edge",
                f"f6-dimrows-{det}-s{s:g}",
                {
                    "frame": {"key": "f6_dimrows"},
                    "box": tall,
                    "kw": {"detector": det, "sigma_px": s},
                },
            )
        add(
            "measure_edge",
            f"f6-dimrows-{det}-validfraction",
            {
                "frame": {"key": "f6_dimrows"},
                "box": tall,
                "kw": {"detector": det, "min_valid_fraction": 0.9},
            },
        )
    for kw in (
        {"channel": "green"},
        {"detector": "sobel"},
        {"sigma_px": 0.0},
        {"sigma_px": NAN},
        {"axis": "z"},
        {"channel": "red", "bayer": "XXXX"},
    ):
        add(
            "measure_edge",
            "error-" + "-".join(f"{k}={v}" for k, v in kw.items()),
            {"frame": {"key": "f1"}, "box": b1["left"], "kw": kw},
        )
    add(
        "measure_edge",
        "error-ndim",
        {"frame": {"key": "asym_profile"}, "box": [0, 0, 16, 16], "kw": {}},
    )

    # -- snap_box -----------------------------------------------------------
    for i, box in enumerate(
        [
            [38, 60, 48, 48],
            [37.9, 61.2, 47.8, 49.9],
            [5, 7, 3, 3],
            [0, 0, 16, 16],
            [-3, -5, 20.5, 31],
            [101, 203, 15, 17],
            [64, 64, 1000, 1000],
        ]
    ):
        add("snap_box", f"box{i}", {"box": box})
    add("snap_box", "min-size-40", {"box": [10, 10, 20, 30], "kw": {"min_size": 40}})
    add("snap_box", "min-size-2", {"box": [10, 10, 5, 3], "kw": {"min_size": 2}})

    # -- edge_error / check_fix / edge_noise_um / detector_mismatch ---------
    ca = _ecald()
    for i, (pos, off) in enumerate(
        [(100.0, 0.0), (101.0, 0.0), (99.2, 0.25), (100.4, -0.3), (123.456, 0.0)]
    ):
        add("edge_error", f"a{i}", {"fix": _fixd(pos), "cal": ca, "offset_um": off})
    add(
        "edge_error",
        "neg-slope",
        {"fix": _fixd(51.8, "y"), "cal": _ecal_b(), "offset_um": 0.1},
    )
    add("edge_error", "default-offset", {"fix": _fixd(100.7), "cal": ca})

    good = _fixd(100.2)
    for cid, fix, params in [
        ("ok", good, None),
        ("not-ok-reason", _fixd(100.0, ok=False, reason="saturated"), None),
        ("not-ok-noreason", _fixd(100.0, ok=False), None),
        ("rotated", _fixd(100.0, angle_deg=6.0), None),
        ("rotated-edge", _fixd(100.0, angle_deg=5.0), None),
        ("rotated-neg", _fixd(100.0, angle_deg=-5.5), None),
        ("rotated-loose", _fixd(100.0, angle_deg=6.0), {"max_angle_change_deg": 10.0}),
        ("not-straight", _fixd(100.0, residual_px=3.5), None),
        ("straight-edge", _fixd(100.0, residual_px=3.0), None),
        (
            "not-straight-loose",
            _fixd(100.0, residual_px=3.5),
            {"max_edge_residual_px": 5.0},
        ),
    ]:
        add("check_fix", cid, {"fix": fix, "cal": ca, "params": params})

    for cid, d in [
        ("a", ca),
        ("b", _ecal_b()),
        ("static-floor", _ecald(noise_px=0.0, residual_px=0.0)),
        ("noise-dominates", _ecald(noise_px=0.5, residual_px=0.04)),
        (
            "residual-dominates",
            _ecald(noise_px=0.01, residual_px=0.3, slope_px_per_um=-0.9),
        ),
    ]:
        add("edge_noise_um", cid, {"cal": d})

    one = _fcald(_ecald(detector="gaussderiv", detector_sigma_px=6.0))
    two_mixed = _fcald(_ecald(), _ecal_b(detector="halfmax"))
    for cid, cal, det, sig in [
        ("gd-6-match", one, "gaussderiv", 6.0),
        ("gd-6-default-sigma", one, "gaussderiv", None),
        ("gd-6-vs-halfmax", one, "halfmax", 6.0),
        ("gd-6-vs-9", one, "gaussderiv", 9.0),
        ("gd-6-vs-6.0000001", one, "gaussderiv", 6.0000001),
        ("gd-6-vs-6.01", one, "gaussderiv", 6.01),
        (
            "halfmax-ignores-sigma",
            _fcald(_ecald(detector="halfmax", detector_sigma_px=6.0)),
            "halfmax",
            9.0,
        ),
        ("two-first-mismatch", two_mixed, "halfmax", 6.0),
    ]:
        a: dict[str, Any] = {"cal": cal, "detector": det}
        if sig is not None:
            a["sigma_px"] = sig
        add("detector_mismatch", cid, a)

    # -- calibration fit / persistence --------------------------------------
    p_sweep = [9.0 + 0.25 * i for i in range(9)]  # 9.0 .. 11.0, p0 = 10
    slope, e0 = 1.92, 120.0
    wob = [0.02 * math.sin(2.1 * i) for i in range(9)]
    for det in ("gaussderiv", "halfmax"):
        # measured on the analytic edge shifted by the piezo (frames not stored)
        edges = [
            sweep_measure(det, 100.0 + (p - 10.0) * slope + w)
            for p, w in zip(p_sweep, wob, strict=True)
        ]
        add(
            "fit_edge_calibration",
            f"sweep-{det}",
            {
                "p_um": p_sweep,
                "edge_px": edges,
                "kw": {
                    "box": [38, 60, 48, 48],
                    "axis": "x",
                    "polarity": 1,
                    "p0_um": 10.0,
                    "noise_px": 0.03,
                    "angle_deg": 0.4,
                    "contrast": 1800.0,
                    "detector": det,
                    "detector_sigma_px": 6.0,
                    "edge_width_px": 23.7,
                },
            },
        )
    lin = [e0 + slope * (p - 10.0) + w for p, w in zip(p_sweep, wob, strict=True)]
    base_kw = {"box": [38, 60, 48, 48], "axis": "x", "polarity": 1, "p0_um": 10.0}
    add(
        "fit_edge_calibration",
        "linear-defaults",
        {"p_um": p_sweep, "edge_px": lin, "kw": base_kw},
    )
    add(
        "fit_edge_calibration",
        "explicit-e0-range",
        {
            "p_um": p_sweep,
            "edge_px": lin,
            "kw": {
                **base_kw,
                "e0_px": 119.5,
                "linear_range_um": 0.7,
                "noise_px": 0.05,
                "detector": "gaussderiv",
                "detector_sigma_px": 4.5,
            },
        },
    )
    add(
        "fit_edge_calibration",
        "negative-slope-y",
        {
            "p_um": p_sweep,
            "edge_px": [50.0 - 1.4 * (p - 10.0) for p in p_sweep],
            "kw": {"box": [86, 15, 48, 60], "axis": "y", "polarity": -1, "p0_um": 10.0},
        },
    )
    add(
        "fit_edge_calibration",
        "small-slope-warns",
        {
            "p_um": p_sweep,
            "edge_px": [100.0 + 0.6 * (p - 10.0) for p in p_sweep],
            "kw": base_kw,
        },
    )
    add(
        "fit_edge_calibration",
        "r2-warn",
        {
            "p_um": p_sweep,
            "edge_px": [e + (0.2 if i % 2 else -0.2) for i, e in enumerate(lin)],
            "kw": base_kw,
        },
    )
    add(
        "fit_edge_calibration",
        "nan-points-dropped",
        {
            "p_um": p_sweep,
            "edge_px": [
                lin[0],
                NAN,
                lin[2],
                lin[3],
                NAN,
                lin[5],
                lin[6],
                lin[7],
                lin[8],
            ],
            "kw": base_kw,
        },
    )
    add(
        "fit_edge_calibration",
        "unsorted",
        {"p_um": p_sweep[::-1], "edge_px": lin[::-1], "kw": base_kw},
    )
    add(
        "fit_edge_calibration",
        "error-too-few",
        {"p_um": p_sweep[:4], "edge_px": lin[:4], "kw": base_kw},
    )
    add(
        "fit_edge_calibration",
        "error-no-travel",
        {"p_um": [10.0] * 6, "edge_px": lin[:6], "kw": base_kw},
    )
    add(
        "fit_edge_calibration",
        "error-barely-moves",
        {
            "p_um": p_sweep,
            "edge_px": [100.0 + 0.1 * (p - 10.0) for p in p_sweep],
            "kw": base_kw,
        },
    )
    add(
        "fit_edge_calibration",
        "error-not-linear",
        {
            "p_um": p_sweep,
            "edge_px": [
                100.0 + 0.5 * (p - 10.0) + 1.5 * (-1) ** i
                for i, p in enumerate(p_sweep)
            ],
            "kw": base_kw,
        },
    )
    add(
        "fit_edge_calibration",
        "error-not-monotonic",
        {
            "p_um": p_sweep,
            "edge_px": [e + 1.2 * (-1) ** i for i, e in enumerate(lin)],
            "kw": {**base_kw, "min_r2": 0.0},
        },
    )
    add(
        "fit_edge_calibration",
        "error-shape",
        {"p_um": p_sweep, "edge_px": lin[:5], "kw": base_kw},
    )

    for cid, d in [
        ("full", _ecald()),
        ("nan-width", _ecald(edge_width_px=None)),
        (
            "minimal",
            {
                "kind": "edge",
                "box": [0, 0, 32, 32],
                "axis": "y",
                "polarity": -1,
                "slope_px_per_um": -2.5,
                "e0_px": 7.0,
                "p0_um": 3.0,
            },
        ),
        ("unknown-keys", _ecald(foo=1, bar="x")),
        ("detector-empty", _ecald(detector="")),
        ("sigma-none", _ecald(detector_sigma_px=None)),
        ("error-kind", _ecald(kind="gauss")),
        ("error-slope-zero", _ecald(slope_px_per_um=0.0)),
        ("error-detector", _ecald(detector="sobel")),
        ("error-box", _ecald(box=[1, 2, 3])),
        ("error-axis", _ecald(axis="z")),
        ("error-missing", {"kind": "edge", "box": [0, 0, 32, 32]}),
    ]:
        add("edgecal_roundtrip", cid, {"cal": d})
    add("rezero", "a", {"cal": _ecald(), "e0_px": 101.25, "p0_um": 10.5})

    for cid, d in [
        ("none", None),
        ("empty", {}),
        ("garbage-str", "junk"),
        ("garbage-list", [1, 2]),
        ("old-gaussian-spot", {"kind": "gaussian", "x0": 1.0}),
        ("bad-slope", _ecald(slope_px_per_um=0.0)),
    ]:
        add("load_calibration", cid, {"d": d})

    # -- SpotFocusController -------------------------------------------------
    def U(a_: float, t: float) -> list[Any]:
        return ["update", a_, t]

    scripts = [
        (
            "deadband-rate-integral",
            {},
            50.0,
            None,
            [
                U(0.01, 0.0),
                U(-0.03, 0.1),
                U(0.2, 1.0),
                U(0.2, 1.2),
                U(0.2, 1.6),
                U(-0.1, 2.0),
                U(-0.1, 2.5),
                U(0.04, 3.1),
                U(0.04, 3.6),
                U(0.5, 4.2),
                U(0.8, 4.8),
                U(-0.8, 5.3),
                U(0.031, 6.0),
                U(0.0301, 7.0),
                U(-0.25, 8.0),
            ],
        ),
        (
            "big-error-clamps-step",
            {},
            10.0,
            None,
            [U(3.0, 0.0), U(3.0, 0.4), U(3.0, 0.5), U(-4.0, 1.1)],
        ),
        (
            "linear-range",
            {"max_step_um": 0.4},
            20.0,
            1.0,
            [
                U(0.5, 0.0),
                U(2.0, 1.0),
                U(-1.5, 2.0),
                U(1.0, 3.0),
                U(1.0001, 4.0),
                U(-0.2, 5.0),
            ],
        ),
        (
            "strikes-to-fault",
            {"strikes": 5},
            5.0,
            None,
            [
                ["bad", "r1"],
                ["bad", "r2"],
                ["bad", "r3"],
                ["bad", "r4"],
                ["good"],
                ["bad", "s1"],
                ["bad", "s2"],
                ["bad", "s3"],
                ["bad", "s4"],
                U(0.5, 0.0),
                ["bad", "s5"],
                U(0.5, 1.0),
                ["good"],
                U(0.5, 2.0),
                ["bad", "again"],
            ],
        ),
        (
            "strikes-3-fault-blocks-update",
            {"strikes": 3},
            5.0,
            None,
            [
                ["bad", "a"],
                ["bad", "b"],
                U(0.3, 0.0),
                ["bad", "c"],
                U(0.3, 1.0),
                ["good"],
                U(0.3, 2.0),
            ],
        ),
        (
            "piezo-range-low",
            {"p_min_um": 4.8, "kp": 1.0},
            5.0,
            None,
            [U(0.1, 0.0), U(0.4, 1.0), U(0.1, 2.0)],
        ),
        (
            "piezo-range-high",
            {"p_max_um": 5.3},
            5.0,
            None,
            [U(-0.4, 0.0), U(-0.4, 1.0), U(-0.4, 2.0)],
        ),
        (
            "piezo-range-both",
            {"p_min_um": 0.0, "p_max_um": 150.0},
            100.0,
            None,
            [U(0.4, 0.0), U(-0.4, 1.0)],
        ),
        (
            "max-excursion",
            {"max_excursion_um": 0.9, "max_step_um": 0.5},
            7.0,
            None,
            [
                U(1.0, 0.0),
                U(1.0, 1.0),
                U(1.0, 2.0),
                U(-1.0, 3.0),
            ],
        ),
        (
            "ki-zero",
            {"ki": 0.0, "kp": 0.5},
            0.0,
            None,
            [U(0.2, 0.0), U(0.2, 1.0), U(-0.6, 2.0)],
        ),
        (
            "ki-large-windup",
            {"ki": 0.5, "kp": 0.1, "max_step_um": 0.3},
            0.0,
            None,
            [
                U(0.3, 0.0),
                U(0.3, 1.0),
                U(0.3, 2.0),
                U(0.3, 3.0),
                U(-0.3, 4.0),
                U(-0.3, 5.0),
            ],
        ),
        (
            "rate-zero-guard",
            {"max_rate_hz": 0.0},
            0.0,
            None,
            [U(0.2, 0.0), U(0.2, 1e6), U(0.2, 1e10)],
        ),
        (
            "rate-fast",
            {"max_rate_hz": 20.0, "deadband_um": 0.0},
            0.0,
            None,
            [
                U(0.1, 0.0),
                U(0.1, 0.04),
                U(0.1, 0.06),
                U(0.0, 0.2),
                U(0.1, 0.3),
            ],
        ),
        (
            "tuned",
            {
                "kp": 1.3,
                "ki": 0.2,
                "deadband_um": 0.1,
                "max_step_um": 1.0,
                "max_excursion_um": 10.0,
            },
            12.5,
            2.0,
            [
                U(0.09, 0.0),
                U(0.11, 0.6),
                U(-0.7, 1.2),
                U(1.9, 1.8),
                U(2.1, 2.4),
                U(0.5, 3.0),
            ],
        ),
    ]
    for cid, params, p0, lr, ops in scripts:
        add(
            "controller",
            cid,
            {"params": params, "p_start_um": p0, "linear_range_um": lr, "ops": ops},
        )

    # -- settle -------------------------------------------------------------
    for i, (ex, fps) in enumerate(
        [
            (5.0, 100.0),
            (0.1, 500.0),
            (50.0, 0.0),
            (15.0, 30.0),
            (0.0, 0.0),
            (1000.0, 1.0),
            (5.0, -1.0),
        ]
    ):
        add("settle_time_s", f"c{i}", {"exposure_ms": ex, "fps": fps})
    add(
        "settle",
        "frames-then-time",
        {
            "ops": [
                ["frame", 0.0],
                ["arm", 10.0, 5.0, 100.0],
                ["frame", 10.001],
                ["frame", 10.002],
                ["frame", 10.003],
                ["frame", 10.004],
                ["frame", 10.005],
                ["remaining", 10.0],
                ["remaining", 10.5],
                ["frame", 10.021],
                ["frame", 11.0],
            ]
        },
    )
    add(
        "settle",
        "long-exposure",
        {
            "ops": [
                ["arm", 0.0, 500.0, 2.0],
                ["frame", 0.1],
                ["frame", 0.2],
                ["frame", 0.3],
                ["frame", 0.4],
                ["frame", 0.5],
                ["frame", 1.4],
                ["frame", 1.6],
                ["remaining", 1.0],
                ["remaining", 5.0],
                ["arm", 5.0, 5.0, 100.0],
                ["frame", 5.0],
                ["frame", 5.1],
                ["frame", 5.2],
                ["frame", 5.3],
                ["frame", 5.4],
                ["frame", 5.5],
            ]
        },
    )
    add("settle", "fresh", {"ops": [["frame", 0.0], ["remaining", 0.0]]})

    add("defaults", "defaults", {})
    return cases, arrays


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--legacy-dir",
        required=True,
        help="the legacy asi_z_stack dir holding spot_focus.py etc.",
    )
    ns = ap.parse_args()
    m = load_legacy(ns.legacy_dir)

    def sweep_measure(det: str, e: float) -> float:
        frame = np.tile(_asym_profile(e), (200, 1))
        fix = m.spot_focus.measure_edge(frame, _asym_box(64, 100.0), detector=det)
        assert fix.ok, fix.reason
        return float(fix.position)

    cases, arrays = build_cases(m.synthetic, sweep_measure)
    for c in cases:
        c["expected"] = run_case(m, c, arrays)
    np.savez_compressed(INPUTS_NPZ, **arrays)
    GOLDEN_JSON.write_text(
        json.dumps(
            {"legacy_dir": str(ns.legacy_dir), "cases": cases}, indent=0, allow_nan=True
        )
        + "\n"
    )
    counts: dict[str, int] = {}
    for c in cases:
        counts[c["fn"]] = counts.get(c["fn"], 0) + 1
    print(f"{len(cases)} cases -> {GOLDEN_JSON.name}, {INPUTS_NPZ.name}")
    for fn, n in counts.items():
        print(f"  {fn}: {n}")


if __name__ == "__main__":
    main()
