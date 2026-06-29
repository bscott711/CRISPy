from magicgui.widgets import Container, Label, PushButton, SpinBox
from pymmcore_plus import CMMCorePlus
from qtpy.QtCore import QTimer
from .controller import CrispController

# How often (ms) to poll read-only CRISP telemetry (SNR / Dither Error / State).
# These are hardware sensor values that the device adapter does NOT emit
# `propertyChanged` events for, so they must be polled explicitly.  Polling only
# happens while the panel is visible to avoid loading the Tiger serial port.
_POLL_INTERVAL_MS = 750


class CrispControlPanel(Container):
    def __init__(self, controller: CrispController, mmcore: CMMCorePlus | None = None):
        self.controller = controller
        self.mmcore = mmcore or CMMCorePlus.instance()

        self._setup_widgets()
        super().__init__(widgets=self._widgets, layout="vertical", labels=False)
        self._apply_styles()
        self._connect_signals()

        # Populate telemetry immediately, then poll while visible.
        self._read_and_update()
        self._poll_timer = QTimer(self.native)
        self._poll_timer.setInterval(_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_telemetry)
        self._poll_timer.start()

    def _setup_widgets(self):
        # --- Step 1 Card ---
        self.card1 = Container(layout="vertical", labels=False)
        self.title1 = Label(value="Step 1: Signal Check (Log Cal)")
        self.btn_log_cal = PushButton(text="Run Log Cal")
        self.btn_log_cal.clicked.connect(self.controller.set_log_cal)
        self.btn_log_cal.clicked.connect(self._read_and_update)

        self.snr_val = Label(value="SNR: -- dB")
        self.snr_status = Label(value="⚪ Waiting")

        # CRITICAL FIX: Added labels=False to the row container
        row1 = Container(
            layout="horizontal",
            labels=False,
            widgets=[self.btn_log_cal, self.snr_val, self.snr_status],
        )
        self.card1.extend([self.title1, row1])

        # --- Step 2 Card ---
        self.card2 = Container(layout="vertical", labels=False)
        self.title2 = Label(value="Step 2: Dither Error Check")
        self.btn_dither = PushButton(text="Run Dither")
        self.btn_dither.clicked.connect(self.controller.set_dither)
        self.btn_dither.clicked.connect(self._read_and_update)

        self.error_val = Label(value="Error: --")
        self.error_status = Label(value="⚪ Waiting")

        row2 = Container(
            layout="horizontal",
            labels=False,
            widgets=[self.btn_dither, self.error_val, self.error_status],
        )
        self.card2.extend([self.title2, row2])

        # --- Step 3 Card ---
        self.card3 = Container(layout="vertical", labels=False)
        self.title3 = Label(value="Step 3: Set Gain")
        self.btn_gain = PushButton(text="Run Gain Cal")
        self.btn_gain.clicked.connect(self.controller.set_gain_cal)
        self.btn_gain.clicked.connect(self._read_and_update)

        self.gain_status = Label(value="⚪ Waiting")

        row3 = Container(
            layout="horizontal", labels=False, widgets=[self.btn_gain, self.gain_status]
        )
        self.card3.extend([self.title3, row3])

        # --- Settings Card ---
        self.card_settings = Container(layout="vertical", labels=False)
        self.title_settings = Label(value="Hardware Settings")
        try:
            led_val = int(
                float(self.mmcore.getProperty(self.controller.label, "LED Intensity"))
            )
        except Exception:
            led_val = 50
        self.spin_led = SpinBox(value=led_val, min=0, max=100, label="LED Intensity (%)")
        self.spin_led.changed.connect(self.controller.set_led_intensity)
        self.card_settings.extend([self.title_settings, self.spin_led])

        self._widgets = [self.card1, self.card2, self.card3, self.card_settings]

    def _apply_styles(self):
        # CSS for the "Cards"
        card_css = """
            background-color: rgba(128, 128, 128, 0.05);
            border: 1px solid rgba(128, 128, 128, 0.2);
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 10px;
        """
        title_css = "font-size: 15px; font-weight: bold; background: transparent; border: none; margin-bottom: 8px;"

        # Modern flat button CSS
        btn_css = """
            QPushButton {
                background-color: #2196F3;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: bold;
                min-width: 100px;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
            QPushButton:pressed {
                background-color: #0D47A1;
            }
        """

        # Reset default magicgui/Qt Label borders to kill the "little squares"
        label_css = "background: transparent; border: none; padding: 0px; margin: 0px;"

        for card in [self.card1, self.card2, self.card3, self.card_settings]:
            card.native.setStyleSheet(card_css)

        for title in [self.title1, self.title2, self.title3, self.title_settings]:
            title.native.setStyleSheet(title_css)

        for btn in [self.btn_log_cal, self.btn_dither, self.btn_gain]:
            btn.native.setStyleSheet(btn_css)

        for lbl in [
            self.snr_val,
            self.snr_status,
            self.error_val,
            self.error_status,
            self.gain_status,
        ]:
            lbl.native.setStyleSheet(label_css)

        # Add some breathing room to the main panel
        self.native.setStyleSheet("background: transparent; border: none;")

    def _connect_signals(self):
        def update_props(dev, prop, val):
            if dev != self.controller.label:
                return

            if prop == "Signal Noise Ratio":
                self._update_snr(val)
            elif prop == "Dither Error":
                self._update_error(val)
            elif prop == "CRISP State":
                self._update_state(val)

        self.mmcore.events.propertyChanged.connect(update_props)

    def _poll_telemetry(self):
        """Periodic poll; only touches the serial port while the panel is visible."""
        try:
            visible = self.native.isVisible()
        except RuntimeError:  # native widget already deleted
            return
        if visible:
            self._read_and_update()

    def _read_and_update(self, *args):
        """Force a hardware refresh and update SNR / Dither / State labels.

        The ASI Tiger adapter caches read-only values and does not emit
        ``propertyChanged`` for sensor readouts, so we explicitly request a refresh
        and then read the current values.
        """
        label = self.controller.label
        try:
            # Ask the adapter to re-query the controller for fresh values.
            self.mmcore.setProperty(label, "RefreshPropertyValues", "Yes")
        except Exception:
            pass
        for prop, updater in (
            ("Signal Noise Ratio", self._update_snr),
            ("Dither Error", self._update_error),
            ("CRISP State", self._update_state),
        ):
            try:
                updater(self.mmcore.getProperty(label, prop))
            except Exception:
                pass

    def _update_snr(self, val):
        try:
            snr = float(val)
            self.snr_val.value = f"SNR: {snr:.2f} dB"
            if snr >= 2.0:
                self.snr_status.value = "🟢 Good SNR"
                self.snr_status.native.setStyleSheet(
                    "color: #4caf50; font-weight: bold; background: transparent; border: none;"
                )
            else:
                self.snr_status.value = "🔴 Low SNR"
                self.snr_status.native.setStyleSheet(
                    "color: #f44336; font-weight: bold; background: transparent; border: none;"
                )
        except ValueError:
            self.snr_val.value = f"SNR: {val}"

    def _update_error(self, val):
        try:
            err = float(val)
            self.error_val.value = f"Error: {err:.0f}"
            if err <= 100:
                self.error_status.value = "🟢 Low Error"
                self.error_status.native.setStyleSheet(
                    "color: #4caf50; font-weight: bold; background: transparent; border: none;"
                )
            else:
                self.error_status.value = "🔴 High Error"
                self.error_status.native.setStyleSheet(
                    "color: #f44336; font-weight: bold; background: transparent; border: none;"
                )
        except ValueError:
            self.error_val.value = f"Error: {val}"

    def _update_state(self, val):
        base_css = "background: transparent; border: none; font-weight: bold;"
        # Transient calibration states (orange), locked/good states (green),
        # error states (red); anything else shown neutrally.
        transient = {
            "loG_cal": "⏳ Log Cal Running...",
            "Dither": "⏳ Dither Running...",
            "gain_Cal": "⏳ Gain Cal Running...",
        }
        good = {"In Focus", "Lock", "Ready", "Save to Controller"}
        if val in transient:
            self.gain_status.value = transient[val]
            color = "#FF9800"
        elif val in good:
            self.gain_status.value = f"🟢 {val}"
            color = "#4caf50"
        elif val in ("Error", "Inhibit", "Dim"):
            self.gain_status.value = f"🔴 {val}"
            color = "#f44336"
        else:
            self.gain_status.value = f"State: {val}"
            color = "#9e9e9e"
        self.gain_status.native.setStyleSheet(f"{base_css} color: {color};")
