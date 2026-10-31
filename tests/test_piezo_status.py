"""Tests for the Qt-free piezo spot-lock status mapping (``CRISPy.piezo_status``)."""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from CRISPy import piezo_status as ps
from CRISPy.piezo_status import SpotStatusInputs as In


def _ready(**kw) -> In:
    """Inputs for a calm, calibrated, idle panel; *kw* override fields."""
    base = {"edge_box_set": True, "calibration_present": True}
    base.update(kw)
    return In(**base)


# -- words ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "word"),
    [
        ({}, "Idle"),
        ({"lock_state": "idle"}, "Idle"),
        ({"lock_state": "preview"}, "Idle"),
        ({"busy": "starting"}, "Starting"),
        ({"busy": "calibrating"}, "Calibrating"),
        ({"busy": "zeroing"}, "Zeroing"),
        ({"lock_state": "running"}, "Running"),
        ({"lock_state": "stopped"}, "Stopped"),
        ({"lock_state": "fault"}, "Fault"),
        ({"fault_reason": "worker died"}, "Fault"),
        ({"lock_state": "fault", "busy": "starting"}, "Fault"),
        ({"lock_state": "running", "busy": "zeroing"}, "Zeroing"),
    ],
)
def test_lock_word(kw, word):
    assert ps.lock_word(In(**kw)) == word


@pytest.mark.parametrize(
    ("kw", "word"),
    [
        ({}, "Not open"),
        ({"camera_opening": True}, "Opening"),
        ({"camera_open": True}, "Running"),
        ({"camera_missing": True}, "No camera"),
        ({"camera_error": "boom"}, "Error"),
        ({"camera_open": True, "camera_error": "boom"}, "Error"),
        ({"camera_missing": True, "camera_error": "boom"}, "Error"),
        ({"camera_opening": True, "camera_open": True}, "Opening"),
    ],
)
def test_camera_word(kw, word):
    assert ps.camera_word(In(**kw)) == word


@pytest.mark.parametrize(
    ("kw", "word"),
    [
        ({}, "Off"),
        ({"led_on": True}, "On"),
        ({"crisp_present": True}, "Off"),
        ({"crisp_present": None, "led_on": True}, "On"),
        ({"crisp_present": False}, "No CRISP device"),
        ({"crisp_present": False, "led_on": True}, "No CRISP device"),
        ({"led_error": "x"}, "Error"),
        ({"led_error": "x", "crisp_present": False}, "Error"),
        ({"led_error": "x", "led_on": True}, "Error"),
    ],
)
def test_led_word(kw, word):
    assert ps.led_word(In(**kw)) == word


@pytest.mark.parametrize(("box", "word"), [(False, "Not set"), (True, "Set")])
def test_edge_box_word(box, word):
    assert ps.edge_box_word(In(edge_box_set=box)) == word


@pytest.mark.parametrize(
    ("kw", "word"),
    [
        ({}, "Required"),
        ({"box_changed": True}, "Required"),
        ({"detector_mismatch": "m"}, "Required"),
        ({"calibration_present": True}, "Valid"),
        (
            {"calibration_present": True, "box_changed": True},
            "Stale (edge box changed)",
        ),
        ({"calibration_present": True, "detector_mismatch": "m"}, "Detector mismatch"),
        (
            {
                "calibration_present": True,
                "detector_mismatch": "m",
                "box_changed": True,
            },
            "Detector mismatch",
        ),
    ],
)
def test_calibration_word(kw, word):
    assert ps.calibration_word(In(**kw)) == word


def test_status_words_bundles_the_five():
    inp = In(
        lock_state="running",
        camera_open=True,
        led_on=True,
        edge_box_set=True,
        calibration_present=True,
    )
    assert ps.status_words(inp) == ps.StatusWords(
        lock="Running", camera="Running", led="On", edge_box="Set", calibration="Valid"
    )
    assert dataclasses.is_dataclass(ps.StatusWords)
    with pytest.raises(dataclasses.FrozenInstanceError):
        inp.busy = "x"  # type: ignore[misc]


def test_word_vocabularies_are_the_spec_ones():
    assert ps.LOCK_WORDS == (
        "Idle",
        "Starting",
        "Calibrating",
        "Zeroing",
        "Running",
        "Stopped",
        "Fault",
    )
    assert ps.CAMERA_WORDS == ("Not open", "Opening", "Running", "No camera", "Error")
    assert ps.LED_WORDS == ("On", "Off", "No CRISP device", "Error")
    assert ps.BOX_WORDS == ("Not set", "Set")
    assert ps.CAL_WORDS == (
        "Required",
        "Valid",
        "Stale (edge box changed)",
        "Detector mismatch",
    )


# -- tracking error -------------------------------------------------------------


@pytest.mark.parametrize(
    ("err", "text"),
    [
        (0.012, "+0.012 µm"),
        (-0.0004, "-0.000 µm"),
        (1.5, "+1.500 µm"),
        (0.0, "+0.000 µm"),
    ],
)
def test_tracking_error_formats_while_running(err, text):
    assert (
        ps.tracking_error_text(In(lock_state="running", tracking_error_um=err)) == text
    )


@pytest.mark.parametrize(
    "kw",
    [
        {"lock_state": "idle"},
        {"lock_state": "stopped"},
        {"lock_state": "fault"},
        {"lock_state": "running", "busy": "zeroing"},
        {"busy": "starting"},
        {"busy": "calibrating"},
        {"lock_state": "running", "fault_reason": "x"},
    ],
)
def test_tracking_error_hidden_unless_running(kw):
    inp = In(tracking_error_um=0.012, **kw)
    assert ps.tracking_error_text(inp) == "Not available"


@pytest.mark.parametrize("err", [None, float("nan"), float("inf")])
def test_tracking_error_needs_a_known_value(err):
    inp = In(lock_state="running", tracking_error_um=err)
    assert ps.tracking_error_text(inp) == "Not available"


def test_tracking_error_never_mentions_crisp():
    for state, err in itertools.product(
        ("idle", "running", "stopped", "fault"), (None, 0.1, -2.0)
    ):
        text = ps.tracking_error_text(
            In(lock_state=state, tracking_error_um=err, crisp_present=False)
        )
        assert "crisp" not in text.lower()


# -- message --------------------------------------------------------------------

# One input per priority level, each the only problem present (apart from the
# fields it needs). The list is in priority order, worst first.
_LEVELS = [
    (
        "fault",
        {"lock_state": "fault", "fault_reason": "worker died"},
        "Fault: worker died",
    ),
    ("start", {"start_error": "no camera"}, "Could not start: no camera"),
    ("calibrate", {"calibrate_error": "no edge"}, "Calibration failed: no edge"),
    ("zero", {"zero_error": "no frame"}, "Zero failed: no frame"),
    ("stop", {"stop_error": "busy"}, "Stop failed: busy"),
    ("camera", {"camera_error": "lost"}, "Camera: lost"),
    ("led", {"led_error": "serial"}, "LED: serial"),
    ("nobox", {"edge_box_set": False}, ps.NEED_BOX_TEXT),
    (
        "mismatch",
        {
            "detector_mismatch": "calibrated with halfmax; the detector setting is "
            "gaussderiv: recalibrate"
        },
        "calibrated with halfmax; the detector setting is gaussderiv: recalibrate",
    ),
    (
        "boxchanged",
        {"box_changed": True},
        "edge box changed since the calibration; recalibrate",
    ),
    ("nocrisp", {"crisp_present": False}, "no CRISP device (no CRISP label)"),
    (
        "mda",
        {"mda_running": True},
        "an acquisition is running; calibrate after it ends",
    ),
    ("calfirst", {"calibration_present": False}, "Calibrate first"),
    ("frames", {"no_new_frames": True}, "No new frames"),
]


def _merge(names: list[str]) -> In:
    kw: dict = {"edge_box_set": True, "calibration_present": True}
    by_name = {n: k for n, k, _ in _LEVELS}
    for n in names:
        kw.update(by_name[n])
    return In(**kw)


@pytest.mark.parametrize(
    ("name", "kw", "text"), _LEVELS, ids=[n for n, _, _ in _LEVELS]
)
def test_each_message_alone(name, kw, text):
    base = {"edge_box_set": True, "calibration_present": True}
    base.update(kw)
    assert ps.message(In(**base)) == text


@pytest.mark.parametrize(
    ("hi", "lo"),
    [
        (_LEVELS[i][0], _LEVELS[j][0])
        for i in range(len(_LEVELS))
        for j in range(i + 1, len(_LEVELS))
        # A recalibrate hint needs a calibration, which "calfirst" removes: the two
        # cannot be present together.
        if not (
            _LEVELS[i][0] in ("mismatch", "boxchanged") and _LEVELS[j][0] == "calfirst"
        )
    ],
)
def test_earlier_item_beats_later_item(hi, lo):
    # NEGATIVE CONTROL (a): every ordered pair. If two priority branches in
    # ``message`` were swapped, the pair of those two would return the lower item's
    # text and fail here.
    expect = {n: t for n, _, t in _LEVELS}[hi]
    assert ps.message(_merge([lo, hi])) == expect
    assert ps.message(_merge([hi, lo])) == expect


def test_everything_at_once_shows_the_fault():
    names = [n for n, _, _ in _LEVELS if n != "calfirst"]
    assert ps.message(_merge(names)) == "Fault: worker died"


def test_fault_without_reason():
    assert ps.message(In(lock_state="fault", edge_box_set=True)) == "Fault"


@pytest.mark.parametrize(
    ("kw", "text"),
    [
        ({"busy": "starting"}, "Starting..."),
        ({"busy": "calibrating"}, "Calibrating..."),
        ({"busy": "zeroing"}, "Zeroing..."),
        ({"busy": "starting", "calibration_present": False}, "Starting..."),
        ({"lock_state": "running"}, "Locked"),
        ({"lock_state": "idle"}, "Ready to lock"),
        ({"lock_state": "stopped"}, "Ready to lock"),
        ({"lock_state": "running", "mda_running": True}, "Locked"),
        ({"busy": "calibrating", "mda_running": True}, "Calibrating..."),
        ({"lock_state": "running", "no_new_frames": True}, "No new frames"),
    ],
)
def test_busy_locked_ready(kw, text):
    assert ps.message(_ready(**kw)) == text


def test_ready_only_when_nothing_else():
    assert ps.message(_ready()) == "Ready to lock"
    for name, _kw, _ in _LEVELS:
        assert ps.message(_merge([name])) != "Ready to lock", name


def test_recalibrate_hints_need_a_calibration():
    # A stale flag without a calibration is not a recalibrate message.
    inp = In(edge_box_set=True, box_changed=True, detector_mismatch="m")
    assert ps.message(inp) == "Calibrate first"


def test_message_is_one_line():
    for names in itertools.chain.from_iterable(
        itertools.combinations([n for n, _, _ in _LEVELS], r) for r in (1, 2)
    ):
        assert "\n" not in ps.message(_merge(list(names)))


# -- single edge box ------------------------------------------------------------


def test_inputs_have_no_second_edge_field():
    for f in dataclasses.fields(In):
        low = f.name.lower()
        assert "edge2" not in low and "box2" not in low and "second" not in low
        assert not low.endswith("_2")


def test_two_edge_calibration_is_stale():
    # The caller reports box_changed when the calibration's edge count is not 1.
    inp = _ready(box_changed=True)
    assert ps.calibration_word(inp) == "Stale (edge box changed)"
    assert ps.message(inp) == "edge box changed since the calibration; recalibrate"


def test_no_text_mentions_a_second_edge():
    forbidden = (
        "edge 2",
        "box 2",
        "edge2",
        "box2",
        "using edge 1",
        "disagree",
        "boxes",
    )
    flags = [{"lock_state": s} for s in ("idle", "running", "stopped", "fault")] + [
        {"busy": b} for b in ("starting", "calibrating", "zeroing")
    ]
    for kw, extra in itertools.product(
        flags,
        [{}, {"box_changed": True}, {"detector_mismatch": "m"}, {"mda_running": True}],
    ):
        inp = _ready(tracking_error_um=0.01, **kw, **extra)
        texts = [
            ps.message(inp),
            ps.tracking_error_text(inp),
            *dataclasses.astuple(ps.status_words(inp)),
        ]
        for t in texts:
            assert not any(w in t.lower() for w in forbidden), t


# -- joystick -------------------------------------------------------------------


@pytest.mark.parametrize(("active", "enabled"), [(True, False), (False, True)])
def test_joystick_for_lock(active, enabled):
    # NEGATIVE CONTROL (b): an inverted ``joystick_for_lock`` (``return
    # lock_active``) fails both rows.
    assert ps.joystick_for_lock(active) is enabled


# -- Qt-free --------------------------------------------------------------------


def test_module_imports_no_qt():
    import sys

    src = ps.__file__
    assert src is not None
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert "qtpy" not in text and "PyQt" not in text and "PySide" not in text
    assert "CRISPy.piezo_status" in sys.modules
