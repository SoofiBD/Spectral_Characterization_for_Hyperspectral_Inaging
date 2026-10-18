"""Monochromator control panel."""

from __future__ import annotations

import datetime as dt

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

from devices.monochromator import (BAUDRATE, BAUDRATES, MonochromatorController, SIMULATED_PORT,
                                   list_serial_ports)
from gui.widgets import NumberSpinBox, committed, hint, set_role
from gui.workers import run_task


class MonochromatorPanel(QWidget):
    traffic = Signal(str, str)          # direction, text (thread-safe bridge)
    connection_changed = Signal(bool)
    log_message = Signal(str)

    def __init__(self, controller: MonochromatorController, simulation_enabled, parent=None):
        super().__init__(parent)
        self.ctrl = controller
        self.simulation_enabled = simulation_enabled
        self._busy = False
        self.ctrl.traffic_callback = lambda d, t: self.traffic.emit(d, t)
        self.traffic.connect(self._on_traffic)
        self._external_busy = False
        self._build_ui()
        self.refresh_ports()
        self._update_state()

    def _build_ui(self):
        conn = QGroupBox("Connection")
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(180)
        self.baud_combo = QComboBox()
        self.baud_combo.addItems([str(b) for b in BAUDRATES])
        self.baud_combo.setCurrentText(str(BAUDRATE))
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setToolTip("Rescan serial ports")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("accent")
        self.status_lbl = QLabel("DISCONNECTED")
        self.status_lbl.setObjectName("statusIdle")
        self.pos_lbl = QLabel("— nm")
        self.pos_lbl.setObjectName("bigReadout")
        h = QHBoxLayout()
        h.addWidget(QLabel("Port:"))
        h.addWidget(self.port_combo, 1)
        h.addWidget(QLabel("Baud:"))
        h.addWidget(self.baud_combo)
        h.addWidget(self.refresh_btn)
        h.addWidget(self.connect_btn)
        cl = QVBoxLayout(conn)
        cl.addLayout(h)
        f = QFormLayout()
        f.addRow("Status:", self.status_lbl)
        f.addRow("Position:", self.pos_lbl)
        cl.addLayout(f)

        # Commands that move the grating are separated from those that only talk to the controller.
        motion = QGroupBox("Motion — moves the grating")
        self.goto_spin = NumberSpinBox(decimals=2, minimum=0, maximum=2000, value=550, suffix=" nm")
        self.goto_btn = QPushButton("GOTO")
        self.goto_btn.setToolTip("Move the grating to this wavelength")
        self.home_btn = QPushButton("HOME")
        self.home_btn.setToolTip("Move the grating to its home position")
        self.set_spin = NumberSpinBox(decimals=2, minimum=0, maximum=2000, value=550, suffix=" nm")
        self.set_btn = QPushButton("SET")
        self.set_btn.setToolTip("Firmware v2: declare the current position without moving")
        self.reset_spin = NumberSpinBox(decimals=2, minimum=0, maximum=2000, value=0, suffix=" nm")
        self.reset_btn = QPushButton("RESET")
        self.reset_btn.setToolTip("Moves the grating BACK by this amount, then calls that position 0")
        self.pos_btn = QPushButton("POS")
        self.pos_btn.setToolTip("Read the position reported by the controller")
        self.status_btn = QPushButton("STATUS")
        self.status_btn.setToolTip("Read the controller status")
        for b in (self.goto_btn, self.home_btn, self.reset_btn):
            b.setObjectName("motion")
        g = QGridLayout(motion)
        g.addWidget(QLabel("Go to:"), 0, 0)
        g.addWidget(self.goto_spin, 0, 1)
        g.addWidget(self.goto_btn, 0, 2)
        g.addWidget(QLabel("Move back & zero:"), 1, 0)
        g.addWidget(self.reset_spin, 1, 1)
        g.addWidget(self.reset_btn, 1, 2)
        g.addWidget(self.home_btn, 2, 2)

        calib = QGroupBox("Position — no motion")
        cg = QGridLayout(calib)
        cg.addWidget(QLabel("Current position is:"), 0, 0)
        cg.addWidget(self.set_spin, 0, 1)
        cg.addWidget(self.set_btn, 0, 2)
        row = QHBoxLayout()
        for b in (self.pos_btn, self.status_btn):
            row.addWidget(b)
        cg.addLayout(row, 1, 0, 1, 3)
        cg.addWidget(hint("Compare the reported position with the physical counter; fix a mismatch with SET."),
                     2, 0, 1, 3)

        manual = QGroupBox("Manual command")
        self.cmd_edit = QLineEdit(placeholderText="e.g. GOTO:550, SET:550, HOME, POS, STATUS")
        self.send_btn = QPushButton("Send")
        mh = QHBoxLayout(manual)
        mh.addWidget(self.cmd_edit, 1)
        mh.addWidget(self.send_btn)

        hist = QGroupBox("Command history")
        self.history = QListWidget()
        QVBoxLayout(hist).addWidget(self.history)

        resp = QGroupBox("Device responses")
        self.console = QPlainTextEdit(readOnly=True)
        self.console.setObjectName("console")
        self.console.setMaximumBlockCount(5000)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.console.clear)
        rl = QVBoxLayout(resp)
        rl.addWidget(self.console)
        rl.addWidget(clear, 0, Qt.AlignRight)
        self.history.setToolTip("Double-click a command to copy it into the manual command field")

        left = QVBoxLayout()
        left.addWidget(conn)
        left.addWidget(motion)
        left.addWidget(calib)
        left.addWidget(manual)
        left.addWidget(hist, 1)
        root = QHBoxLayout(self)
        root.addLayout(left, 1)
        root.addWidget(resp, 1)

        self.refresh_btn.clicked.connect(self.refresh_ports)
        self.connect_btn.clicked.connect(self._toggle_connect)
        self.goto_btn.clicked.connect(lambda: self._run("GOTO", self.ctrl.goto, committed(self.goto_spin)))
        self.home_btn.clicked.connect(lambda: self._run("HOME", self.ctrl.home))
        self.set_btn.clicked.connect(lambda: self._run("SET", self.ctrl.set_position, committed(self.set_spin)))
        self.reset_btn.clicked.connect(self._confirm_reset)
        self.pos_btn.clicked.connect(lambda: self._run("POS", self.ctrl.get_position))
        self.status_btn.clicked.connect(lambda: self._run("STATUS", self.ctrl.status))
        self.send_btn.clicked.connect(self._send_manual)
        self.cmd_edit.returnPressed.connect(self._send_manual)
        self.history.itemDoubleClicked.connect(lambda it: self.cmd_edit.setText(it.text().split("  ", 1)[-1]))

    def refresh_ports(self):
        current = self.port_combo.currentText()
        self.port_combo.clear()
        self.port_combo.addItems(list_serial_ports())
        self.port_combo.addItem(SIMULATED_PORT)
        if self.simulation_enabled():
            self.port_combo.setCurrentText(SIMULATED_PORT)
        elif current:
            self.port_combo.setCurrentText(current)

    def _append(self, text: str, color: str = "#d0d7de"):
        ts = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.console.appendHtml(f'<span style="color:#6e7781">{ts}</span> '
                                f'<span style="color:{color}">{text}</span>')

    def _on_traffic(self, direction: str, text: str):
        if direction == ">":
            self._append(f"&gt;&gt; {text}", "#58a6ff")
        else:
            color = "#ff7b72" if text.upper().startswith("ERROR") else "#7ee787"
            self._append(f"&lt;&lt; {text}", color)

    def _update_state(self):
        c = self.ctrl.is_connected
        self.connect_btn.setText("Disconnect" if c else "Connect")
        self.connect_btn.setEnabled(not self._busy and not self._external_busy)
        self.port_combo.setEnabled(not c and not self._external_busy)
        self.baud_combo.setEnabled(not c)
        self.refresh_btn.setEnabled(not c)
        for w in (self.goto_btn, self.home_btn, self.reset_btn, self.set_btn, self.pos_btn, self.status_btn,
                  self.send_btn, self.cmd_edit):
            w.setEnabled(c and not self._busy and not self._external_busy)
        if not c:
            self._set_status("DISCONNECTED", bad=True)
            self.pos_lbl.setText("— nm")

    def _set_status(self, text: str, bad=False, busy=False):
        self.status_lbl.setText(text)
        set_role(self.status_lbl, "statusIdle" if text == "DISCONNECTED" else
                 "statusBad" if bad else "statusBusy" if busy else "statusOk")

    def set_external_busy(self, busy: bool):
        """Lock manual controls while an automated scan owns the device."""
        self._external_busy = busy
        self._update_state()

    def update_position(self, nm: float | None):
        if nm is not None:
            self.pos_lbl.setText(f"{nm:.2f} nm")

    def _toggle_connect(self):
        if self.ctrl.is_connected:
            self.ctrl.disconnect()
            self._append("Disconnected", "#d29922")
            self._update_state()
            self.connection_changed.emit(False)
            return
        port = self.port_combo.currentText()
        simulate = self.simulation_enabled() or port == SIMULATED_PORT
        self.connect_btn.setEnabled(False)
        self._set_status("CONNECTING…", busy=True)

        def done(_):
            self._append(f"Connected to {self.ctrl.port}", "#d29922")
            self._update_state()
            self._set_status("READY" + (" (SIM)" if self.ctrl.is_simulated else ""))
            self.connection_changed.emit(True)
            self.log_message.emit(f"Monochromator connected on {self.ctrl.port}")
            self._run("POS", self.ctrl.get_position)

        def fail(msg):
            self._append(f"Connection failed: {msg}", "#ff7b72")
            self._update_state()
            self.log_message.emit(f"Monochromator connection failed: {msg}")

        run_task(self.ctrl.connect, port, simulate, baudrate=int(self.baud_combo.currentText()),
                 on_result=done, on_error=fail,
                 on_finished=self._update_state)

    def _run(self, label: str, fn, *args):
        self._busy = True
        self._update_state()
        self._set_status(f"BUSY: {label}", busy=True)
        self.history.addItem(f"{dt.datetime.now():%H:%M:%S}  {label}{(':%g' % args[0]) if args else ''}")
        self.history.scrollToBottom()

        def ok(result):
            if isinstance(result, float):
                self.update_position(result)
            elif isinstance(result, list):
                self.update_position(self.ctrl.position_nm)
            self._set_status("READY" + (" (SIM)" if self.ctrl.is_simulated else ""))

        def fail(msg):
            self._append(f"ERROR: {msg}", "#ff7b72")
            self._set_status("ERROR", bad=True)
            self.log_message.emit(f"Monochromator: {msg}")

        def fin():
            self._busy = False
            self._update_state()

        run_task(fn, *args, on_result=ok, on_error=fail, on_finished=fin)

    def _confirm_reset(self):
        from PySide6.QtWidgets import QMessageBox
        back = committed(self.reset_spin)
        if QMessageBox.question(
                self, "RESET",
                f"RESET moves the grating BACK by {back:g} nm and then calls that position 0.\n"
                "To only tell the controller where it is, use SET instead.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self._run("RESET", self.ctrl.reset, back)

    def _send_manual(self):
        cmd = self.cmd_edit.text().strip()
        if not cmd:
            return
        self.cmd_edit.clear()
        self._busy = True
        self._update_state()
        self._set_status(f"BUSY: {cmd}", busy=True)
        self.history.addItem(f"{dt.datetime.now():%H:%M:%S}  {cmd}")
        self.history.scrollToBottom()

        def ok(_lines):
            self.update_position(self.ctrl.position_nm)
            self._set_status("READY" + (" (SIM)" if self.ctrl.is_simulated else ""))

        def fail(msg):
            self._append(f"ERROR: {msg}", "#ff7b72")
            self._set_status("ERROR", bad=True)

        def fin():
            self._busy = False
            self._update_state()

        run_task(self.ctrl.send_raw, cmd, on_result=ok, on_error=fail, on_finished=fin)
