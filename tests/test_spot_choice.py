"""Which panel is the camera lock panel (pure logic, no Qt)."""

from CRISPy.spot_choice import choose_spot_index

P34 = ("piezo", "PiezoStage:P:34")
P35 = ("piezo", "PiezoStage:P:35")
Z32 = ("stage", "ZStage:Z:32")


def test_no_piezo_panel_gets_none() -> None:
    assert choose_spot_index([Z32], None) is None
    assert choose_spot_index([], None) is None
    assert choose_spot_index([("unknown", None)], "PiezoStage:P:34") is None


def test_first_piezo_without_override() -> None:
    assert choose_spot_index([Z32, P34, P35], None) == 1


def test_override_picks_the_matching_piezo() -> None:
    assert choose_spot_index([P34, Z32, P35], "PiezoStage:P:35") == 2


def test_override_that_matches_nothing_falls_back_to_first_piezo() -> None:
    assert choose_spot_index([Z32, P34, P35], "PiezoStage:P:99") == 1


def test_override_never_picks_a_stage_panel() -> None:
    assert choose_spot_index([Z32, P34], "ZStage:Z:32") == 1
