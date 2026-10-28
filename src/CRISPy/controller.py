from __future__ import annotations

from typing import Protocol

from pymmcore_plus import CMMCorePlus, DeviceType

from .discovery import ActuatorType, classify_focus_actuator, find_paired_actuator_label

# The ASITiger adapter's settable "CRISP State" values (exact strings matter).
STATE_PROP = "CRISP State"
LED_PROP = "LED Intensity"
# The ASITiger hub forwards a raw command string via its "SerialCommand" property.
# Tiger cards are addressed by a one-character prefix (the card's hex address
# 0x31..0x39 is the ASCII digit '1'..'9'); without it the command goes to the
# default card, which is why an unaddressed ``LK F=90`` only ever lit the Z axis.
# The hub skips a command identical to its previous one unless
# "OnlySendSerialCommandOnChange" is "No", so the LED writes force that.
# The hub is found via ``getParentLabel``; a rig config without "Parent,..." lines
# reports "" there (the adapter finds its hub by MMCore's same-module inference,
# which does not set the parent ID), so the fallback is the one loaded Hub device of
# the CRISP's own library that has a SerialCommand property.
# LED Off is raw too (not the adapter's Idle): the adapter skips a "CRISP State"
# write equal to its cached state, and a raw ``LK F=90`` from Idle leaves that cache
# at Idle, so ``set_idle()`` would be a no-op and the LED would stay lit.
HUB_SERIAL_COMMAND_PROP = "SerialCommand"
HUB_ONLY_SEND_CHANGED_PROP = "OnlySendSerialCommandOnChange"
LED_ON_COMMAND = "LK F=90"
LED_OFF_COMMAND = "LK F=79"
# CRISP states in which a changed LED intensity should be re-applied by re-lighting
# the LED (see should_reapply_led).
LED_REAPPLY_STATES = ("Idle", "Ready")
# The wheel/knob bound to an ASITiger stage's manual input is a regular device
# property on the *stage* (not the CRISP device): e.g. PiezoStage:P:34's
# "JoystickInput" = "22 - right wheel". This project's rig encodes the
# correct per-axis binding in one Micro-Manager config preset (see
# set_joystick_enabled), rather than a raw ASI "J" serial command -- a raw
# "J <axis>=<code>" command changes the hardware but not the ASITiger
# adapter's own cached property value, which is unreliable (confirmed on the
# bench: it silently failed to restore a custom wheel binding on re-enable).
JOYSTICK_PROP = "JoystickInput"
JOYSTICK_OFF_VALUE = "0 - none"
JOYSTICK_GROUP = "Joystick Wheels"
JOYSTICK_PRESET = "Z_P"
# Per-axis calibration read from the controller; these define the mapping between
# the raw error signal (controller counts) and physical focus motion, and differ
# between actuators (a piezo and a Z stage calibrate to very different gains).
CAL_GAIN_PROP = "Calibration Gain"
CAL_RANGE_PROP = "Calibration Range(um)"
# Lock-offset setpoint: shifting it makes the servo hold a deliberately
# defocused plane while staying locked (e.g. a per-wavelength correction for
# axial chromatic aberration). LOCK_OFFSET_PROP is the read-only offset the
# servo is currently tracking; SET_LOCK_OFFSET_PROP is the writable target.
# Both are in the same controller counts as the error signal.
LOCK_OFFSET_PROP = "Lock Offset"
SET_LOCK_OFFSET_PROP = "Set Lock Offset (Advanced Users Only)"


def counts_per_um(gain: float | None, range_um: float | None) -> float | None:
    """Error counts per micron of defocus: ``|gain| / range_um``.

    ``None`` if either calibration value is unavailable or the range is
    non-positive. Shared by :class:`ASICrispController` and the UI, which already
    holds the gain/range values from its telemetry read and so needn't re-read them.
    """
    if gain is None or range_um is None or range_um <= 0:
        return None
    return abs(gain) / range_um


def should_reapply_led(state: str, led_on: bool) -> bool:
    """Whether to re-light the LED after its intensity was changed.

    The firmware applies a new ``UL X=`` (LED intensity) only when the LED is
    (re)lit -- a rig finding -- so after an intensity change a lit LED has to be
    switched on again to take effect. That is safe only when CRISP is not driving
    the actuator: in Idle or Ready (:data:`LED_REAPPLY_STATES`). While locked, locking,
    calibrating or dithering, re-lighting could disturb the servo, so the user must
    unlock (or re-lock) instead.

    Parameters
    ----------
    state : str
        The CRISP state as read from the adapter (e.g. ``"Idle"``, ``"Lock"``).
    led_on : bool
        Whether the panel considers this axis's LED to be on.

    Returns
    -------
    bool
        ``True`` only when *led_on* and *state* is ``"Idle"`` or ``"Ready"``; never
        for locked, calibrating, Dither, empty or unknown states.
    """
    return led_on and state in LED_REAPPLY_STATES


class CrispController(Protocol):
    @property
    def label(self) -> str: ...
    @property
    def actuator_type(self) -> ActuatorType: ...
    @property
    def actuator_label(self) -> str | None: ...
    def set_state(self, state: str) -> None: ...
    def set_log_cal(self) -> None: ...
    def set_dither(self) -> None: ...
    def set_gain_cal(self) -> None: ...
    def set_idle(self) -> None: ...
    def set_ready(self) -> None: ...
    def set_lock(self) -> None: ...
    def set_unlock(self) -> None: ...
    def set_reset_offset(self) -> None: ...
    def set_save(self) -> None: ...
    def set_led_intensity(self, value: int) -> None: ...
    def set_led(self, on: bool) -> None: ...
    def set_joystick_enabled(self, enabled: bool) -> None: ...
    def get_state(self) -> str: ...
    def get_calibration_gain(self) -> float | None: ...
    def get_calibration_range_um(self) -> float | None: ...
    def get_sensitivity_counts_per_um(self) -> float | None: ...
    def get_lock_offset_counts(self) -> float | None: ...
    def get_set_lock_offset_counts(self) -> float | None: ...
    def set_lock_offset_counts(self, counts: int) -> None: ...
    def set_lock_offset_um(
        self, um: float, counts_per_um: float | None = None
    ) -> None: ...
    def get_lock_offset_um(
        self, counts_per_um: float | None = None
    ) -> float | None: ...


class ASICrispController:
    def __init__(self, label: str, mmcore: CMMCorePlus | None = None):
        self._label = label
        self._mmcore = mmcore or CMMCorePlus.instance()
        # Resolve the paired actuator once; it does not change while loaded.
        self._actuator_type: ActuatorType = classify_focus_actuator(label, self._mmcore)
        # The stage device whose "JoystickInput" property this axis's wheel
        # lives on (e.g. PiezoStage:P:34), and the correct "on" value for it
        # per the user's own JOYSTICK_PRESET -- see set_joystick_enabled.
        self._joystick_stage_label = find_paired_actuator_label(label, self._mmcore)
        self._joystick_on_value = self._read_joystick_preset_value()
        # The Tiger hub carrying this axis's raw commands; resolved lazily on the
        # first LED write so construction has no side effects (see _hub_label).
        self._hub: str | None = None

    @property
    def label(self) -> str:
        return self._label

    @property
    def actuator_type(self) -> ActuatorType:
        """``"piezo"``, ``"stage"``, or ``"unknown"`` for the paired focus device."""
        return self._actuator_type

    @property
    def actuator_label(self) -> str | None:
        """Label of the stage device this axis drives (e.g. ``PiezoStage:P:34``).

        ``None`` if no ASITiger stage with the same axis letter and hex address is
        loaded.
        """
        return self._joystick_stage_label

    def set_state(self, state: str) -> None:
        """Write an exact ASITiger ``CRISP State`` value (e.g. ``"Lock"``)."""
        self._mmcore.setProperty(self._label, STATE_PROP, state)

    # --- calibration workflow ---
    def set_log_cal(self) -> None:
        self.set_state("loG_cal")

    def set_dither(self) -> None:
        self.set_state("Dither")

    def set_gain_cal(self) -> None:
        self.set_state("gain_Cal")

    # --- operation ---
    def set_idle(self) -> None:
        """Idle: servo off, LED off."""
        self.set_state("Idle")

    def set_ready(self) -> None:
        """Ready: LED on, awaiting a lock command."""
        self.set_state("Ready")

    def set_lock(self) -> None:
        """Engage focus lock on this axis (the Tiger card servos it autonomously)."""
        self.set_state("Lock")

    def set_unlock(self) -> None:
        """Release focus lock (returns to Ready, LED stays on)."""
        self.set_state("Ready")

    def set_reset_offset(self) -> None:
        """Re-zero the focus error / offset at the current Z position."""
        self.set_state("Reset Focus Offset")

    def set_save(self) -> None:
        """Persist the current CRISP settings to the controller's NVRAM."""
        self.set_state("Save to Controller")

    def set_led_intensity(self, value: int) -> None:
        self._mmcore.setProperty(self._label, LED_PROP, str(value))

    def _hub_label(self) -> str:
        """Label of the Tiger hub that forwards raw commands for this axis.

        Uses the device's parent label when MMCore has one. A config without
        ``Parent,...`` lines reports ``""`` there, so the fallback is the single
        loaded Hub device from the same library as this CRISP that has a
        ``SerialCommand`` property. Cached after the first success.

        Raises
        ------
        RuntimeError
            If the fallback finds no such hub, or more than one.
        """
        if self._hub is not None:
            return self._hub
        core = self._mmcore
        hub = str(core.getParentLabel(self._label))
        if not hub:
            library = core.getDeviceLibrary(self._label)
            candidates = [
                dev
                for dev in core.getLoadedDevicesOfType(DeviceType.HubDevice)
                if core.getDeviceLibrary(dev) == library
                and core.hasProperty(dev, HUB_SERIAL_COMMAND_PROP)
            ]
            if len(candidates) != 1:
                raise RuntimeError(
                    f"Cannot find the Tiger hub for {self._label}: it has no parent "
                    f"label and {len(candidates)} loaded hubs from its library "
                    f"({library!r}) have a {HUB_SERIAL_COMMAND_PROP!r} property "
                    f"(expected exactly one: {candidates})."
                )
            hub = candidates[0]
        self._hub = hub
        return hub

    def _send_addressed(self, lk_code: str) -> None:
        """Send a raw ``LK`` command to this axis's card through the hub.

        The command goes to the hub's ``SerialCommand`` property (still the shared
        Core), prefixed with the card's address character. The hub's
        "only send on change" flag is forced to ``"No"`` for the write and restored
        afterwards.
        """
        hub = self._hub_label()
        hex_addr = str(self._mmcore.getProperty(self._label, "TigerHexAddress"))
        code = int(hex_addr, 16)
        if not 0x31 <= code <= 0x39:
            raise ValueError(
                f"Unsupported Tiger card address {hex_addr!r} for a raw LED command."
            )
        command = chr(code) + lk_code
        previous = str(self._mmcore.getProperty(hub, HUB_ONLY_SEND_CHANGED_PROP))
        self._mmcore.setProperty(hub, HUB_ONLY_SEND_CHANGED_PROP, "No")
        try:
            self._mmcore.setProperty(hub, HUB_SERIAL_COMMAND_PROP, command)
        finally:
            self._mmcore.setProperty(hub, HUB_ONLY_SEND_CHANGED_PROP, previous)

    def set_led(self, on: bool) -> None:
        """Turn this axis's LED on or off without touching the other axis.

        Neither direction has a usable ``CRISP State`` value (the adapter skips a
        state write equal to its cached one, so Idle can be a no-op while the LED is
        lit), so both are raw addressed commands sent through the hub's
        ``SerialCommand`` property: ``LK F=90`` for on, ``LK F=79`` for off. The hub
        is found even when the CRISP device has no parent label (see
        :meth:`_hub_label`).

        Parameters
        ----------
        on : bool
            ``True`` to light the LED, ``False`` to switch it off.
        """
        self._send_addressed(LED_ON_COMMAND if on else LED_OFF_COMMAND)

    def get_state(self) -> str:
        try:
            return str(self._mmcore.getProperty(self._label, STATE_PROP))
        except Exception:
            return ""

    # --- joystick lockout ---
    def _read_joystick_preset_value(self) -> str | None:
        """Look up this axis's correct ``JoystickInput`` value from JOYSTICK_PRESET.

        Reading the "on" target from the user's own saved preset (rather than
        snapshotting whatever the property currently reads) means a wheel that
        is already mis-bound -- e.g. from a prior bug -- self-corrects the
        next time this axis unlocks, instead of that wrong state being
        preserved indefinitely.
        """
        if self._joystick_stage_label is None:
            return None
        try:
            settings = self._mmcore.getConfigData(JOYSTICK_GROUP, JOYSTICK_PRESET)
        except Exception:
            return None
        for dev, prop, value in settings:
            if dev == self._joystick_stage_label and prop == JOYSTICK_PROP:
                return str(value)
        return None

    def set_joystick_enabled(self, enabled: bool) -> None:
        """Enable/disable manual joystick/wheel input on this axis's actuator.

        Writes the ASITiger ``JoystickInput`` device property directly on the
        paired stage (through the Core, like any other property), restoring
        the exact value encoded in the user's ``JOYSTICK_PRESET`` config
        preset -- which already correctly encodes their wheel layout (e.g.
        left wheel -> Z stage, right wheel -> piezo) -- rather than a raw ASI
        serial command. No-ops silently if the paired stage device or a
        matching preset entry could not be resolved.
        """
        if self._joystick_stage_label is None:
            return
        if not enabled:
            self._mmcore.setProperty(
                self._joystick_stage_label, JOYSTICK_PROP, JOYSTICK_OFF_VALUE
            )
            return
        if self._joystick_on_value is not None:
            self._mmcore.setProperty(
                self._joystick_stage_label, JOYSTICK_PROP, self._joystick_on_value
            )

    # --- calibration / error scaling ---
    def _get_float(self, prop: str) -> float | None:
        try:
            return float(self._mmcore.getProperty(self._label, prop))
        except Exception:
            return None

    def get_calibration_gain(self) -> float | None:
        """Error-signal calibration gain (controller counts) for this axis."""
        return self._get_float(CAL_GAIN_PROP)

    def get_calibration_range_um(self) -> float | None:
        """Dither calibration travel, in microns, used to derive the gain."""
        return self._get_float(CAL_RANGE_PROP)

    def get_sensitivity_counts_per_um(self) -> float | None:
        """Error counts per micron of defocus: ``|Calibration Gain| / range``.

        This is the actuator-specific scaling that maps the raw error signal onto
        physical focus displacement; it lets a raw error/offset reading be
        expressed in microns and compared across axes with different actuators.
        Returns ``None`` if either calibration value is unavailable or the range
        is non-positive.
        """
        return counts_per_um(
            self.get_calibration_gain(), self.get_calibration_range_um()
        )

    # --- lock offset ---
    def _resolve_counts_per_um(self, counts_per_um: float | None) -> float:
        """Return an explicit *counts_per_um* or fall back to the device value.

        Raises
        ------
        RuntimeError
            If *counts_per_um* is ``None`` and the device's own
            ``|Calibration Gain| / Calibration Range(um)`` is unavailable, so
            no micron<->counts conversion is possible.
        """
        scale = counts_per_um or self.get_sensitivity_counts_per_um()
        if not scale:
            raise RuntimeError(
                f"No counts-per-micron scale available for {self._label} "
                "(Calibration Gain / Range could not be read) and none was "
                "passed explicitly."
            )
        return scale

    def get_lock_offset_counts(self) -> float | None:
        """Read-only offset the servo is currently holding, in controller counts.

        Near zero when locked and in focus; a non-zero steady value means the
        servo is deliberately holding a shifted plane (see
        :meth:`set_lock_offset_counts`). ``None`` if the property can't be read.
        """
        return self._get_float(LOCK_OFFSET_PROP)

    def get_set_lock_offset_counts(self) -> float | None:
        """Read back the *writable* lock-offset setpoint, in controller counts.

        Distinct from :meth:`get_lock_offset_counts` (the read-only tracked
        value): comparing the two detects an adapter that accepted a write into
        its cache without the controller acting on it, or vice versa.
        """
        return self._get_float(SET_LOCK_OFFSET_PROP)

    def set_lock_offset_counts(self, counts: int) -> None:
        """Command an **absolute** lock-offset setpoint, in controller counts.

        The Tiger card servos the focus actuator to hold this offset from the
        calibrated in-focus error value while staying locked. Not a delta --
        each call replaces the previous setpoint. The caller owns waiting for
        the actuator to settle and verifying the result.

        Parameters
        ----------
        counts : int
            Target offset in controller counts (rounded to an int on write).
        """
        self._mmcore.setProperty(
            self._label, SET_LOCK_OFFSET_PROP, str(int(round(counts)))
        )

    def set_lock_offset_um(self, um: float, counts_per_um: float | None = None) -> None:
        """Command an absolute lock-offset setpoint expressed in microns.

        Converts *um* to counts via *counts_per_um* (or the device's own
        sensitivity) and calls :meth:`set_lock_offset_counts`.

        Parameters
        ----------
        um : float
            Target focus offset from the calibrated plane, in microns.
        counts_per_um : float | None
            Conversion scale to use; ``None`` falls back to
            :meth:`get_sensitivity_counts_per_um`. Raises ``RuntimeError`` if
            neither is available.
        """
        scale = self._resolve_counts_per_um(counts_per_um)
        self.set_lock_offset_counts(round(um * scale))

    def get_lock_offset_um(self, counts_per_um: float | None = None) -> float | None:
        """The servo's currently-held lock offset in microns, or ``None``.

        ``None`` if the offset can't be read; raises ``RuntimeError`` if no
        micron<->counts scale is available (pass *counts_per_um* to avoid that).
        """
        counts = self.get_lock_offset_counts()
        if counts is None:
            return None
        return counts / self._resolve_counts_per_um(counts_per_um)
