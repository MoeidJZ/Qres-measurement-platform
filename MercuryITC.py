from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

import qcodes as qc
from qcodes.instrument import VisaInstrument, VisaInstrumentKWArgs
from qcodes.parameters import Parameter
from qcodes.validators import Numbers, Strings

import ipywidgets as widgets
from IPython.display import display

if TYPE_CHECKING:
    from typing_extensions import Unpack


class MercuryiTC(VisaInstrument):
    """
    QCoDeS driver for Oxford Instruments Mercury iTC.

    Default device mapping:
      - VTI sensor / loop:    MB1.T1
      - Sample sensor / loop: DB8.T1
      - Pressure loop:        DB5.P1
      - Needle valve:         DB4.G1
    """

    default_terminator = "\n"
    _NUMERIC_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")

    def __init__(
        self,
        name: str,
        address: str,
        sample_temp_dev: str = "DB8.T1",
        vti_temp_dev: str = "MB1.T1",
        pressure_dev: str = "DB5.P1",
        valve_dev: str = "DB4.G1",
        **kwargs: "Unpack[VisaInstrumentKWArgs]",
    ):
        super().__init__(name, address, terminator=self.default_terminator, **kwargs)

        # Mercury sometimes returns non-ASCII unit symbols such as µ.
        # latin-1 safely decodes byte 0xB5 to 'µ'.
        try:
            self.visa_handle.encoding = "latin-1"
        except Exception:
            pass

        self._sample_temp_dev = sample_temp_dev
        self._vti_temp_dev = vti_temp_dev
        self._pressure_dev = pressure_dev
        self._valve_dev = valve_dev
        self._last_resp = ""

        # ------------------------
        # Sample loop
        # ------------------------
        self.sample_temperature: Parameter = self.add_parameter(
            name="sample_temperature",
            label="Sample temperature",
            unit="K",
            get_cmd=lambda: self._get_val(
                f"READ:DEV:{self._sample_temp_dev}:TEMP:SIG:TEMP"
            ),
        )

        self.sample_temperature_setpoint: Parameter = self.add_parameter(
            name="sample_temperature_setpoint",
            label="Sample temperature setpoint",
            unit="K",
            get_cmd=lambda: self._get_val(
                f"READ:DEV:{self._sample_temp_dev}:TEMP:LOOP:TSET"
            ),
            set_cmd=lambda v: self._set_val(
                f"SET:DEV:{self._sample_temp_dev}:TEMP:LOOP:TSET:{v}"
            ),
            vals=Numbers(min_value=0),
        )

        self.sample_heater_auto: Parameter = self.add_parameter(
            name="sample_heater_auto",
            label="Sample heater auto control",
            get_cmd=lambda: self._get_on_off(
                f"READ:DEV:{self._sample_temp_dev}:TEMP:LOOP:ENAB"
            ),
            set_cmd=lambda v: self._set_val(
                f"SET:DEV:{self._sample_temp_dev}:TEMP:LOOP:ENAB:{v}"
            ),
            val_mapping={"Manual": "OFF", "Auto": "ON"},
        )

        self.sample_heater_power: Parameter = self.add_parameter(
            name="sample_heater_power",
            label="Sample heater power",
            unit="W",
            get_cmd=lambda: self._get_val(
                f"READ:DEV:{self._sample_temp_dev}:HTR:SIG:POWR"
            ),
        )

        # ------------------------
        # VTI loop
        # ------------------------
        self.vti_temperature: Parameter = self.add_parameter(
            name="vti_temperature",
            label="VTI temperature",
            unit="K",
            get_cmd=lambda: self._get_val(
                f"READ:DEV:{self._vti_temp_dev}:TEMP:SIG:TEMP"
            ),
        )

        self.vti_temperature_setpoint: Parameter = self.add_parameter(
            name="vti_temperature_setpoint",
            label="VTI temperature setpoint",
            unit="K",
            get_cmd=lambda: self._get_val(
                f"READ:DEV:{self._vti_temp_dev}:TEMP:LOOP:TSET"
            ),
            set_cmd=lambda v: self._set_val(
                f"SET:DEV:{self._vti_temp_dev}:TEMP:LOOP:TSET:{v}"
            ),
            vals=Numbers(min_value=0),
        )

        self.vti_heater_auto: Parameter = self.add_parameter(
            name="vti_heater_auto",
            label="VTI heater auto control",
            get_cmd=lambda: self._get_on_off(
                f"READ:DEV:{self._vti_temp_dev}:TEMP:LOOP:ENAB"
            ),
            set_cmd=lambda v: self._set_val(
                f"SET:DEV:{self._vti_temp_dev}:TEMP:LOOP:ENAB:{v}"
            ),
            val_mapping={"Manual": "OFF", "Auto": "ON"},
        )

        self.vti_heater_power: Parameter = self.add_parameter(
            name="vti_heater_power",
            label="VTI heater power",
            unit="W",
            get_cmd=lambda: self._get_val(
                f"READ:DEV:{self._vti_temp_dev}:HTR:SIG:POWR"
            ),
        )

        # ------------------------
        # Pressure / valve
        # ------------------------
        self.pressure: Parameter = self.add_parameter(
            name="pressure",
            label="Pressure",
            unit="mB",
            get_cmd=lambda: self._get_val(
                f"READ:DEV:{self._pressure_dev}:PRES:SIG:PRES"
            ),
        )

        self.pressure_setpoint: Parameter = self.add_parameter(
            name="pressure_setpoint",
            label="Pressure setpoint",
            unit="mB",
            get_cmd=lambda: self._get_val(
                f"READ:DEV:{self._pressure_dev}:PRES:LOOP:PRST"
            ),
            set_cmd=lambda v: self._set_val(
                f"SET:DEV:{self._pressure_dev}:PRES:LOOP:PRST:{v}"
            ),
        )

        self.pressure_auto: Parameter = self.add_parameter(
            name="pressure_auto",
            label="Pressure control mode",
            get_cmd=lambda: self._get_on_off(
                f"READ:DEV:{self._pressure_dev}:PRES:LOOP:FAUT"
            ),
            set_cmd=lambda v: self._set_val(
                f"SET:DEV:{self._pressure_dev}:PRES:LOOP:FAUT:{v}"
            ),
            val_mapping={"Manual": "OFF", "Auto": "ON"},
        )

        self.needle_valve: Parameter = self.add_parameter(
            name="needle_valve",
            label="Needle valve opening",
            unit="%",
            get_cmd=lambda: self._get_val(
                f"READ:DEV:{self._valve_dev}:AUX:SIG:PERC"
            ),
            vals=Numbers(min_value=0, max_value=100),
        )

        # ------------------------
        # Debug
        # ------------------------
        self.last_response: Parameter = self.add_parameter(
            name="last_response",
            label="Last raw response",
            get_cmd=self._get_last_response,
            set_cmd=False,
            vals=Strings(),
        )

        self.connect_message()

    # ------------------------
    # Core IO helpers
    # ------------------------

    def _set_val(self, cmd: str) -> None:
        resp = self.ask(cmd)
        self._last_resp = resp

        if resp.endswith("N/A"):
            raise ValueError(
                "A wrong board id was used. Use READ:SYS:CAT to get valid device ids."
            )
        if resp.endswith("INVALID"):
            raise RuntimeError(f"Mercury iTC rejected command: {cmd!r}. Reply: {resp!r}")
        if not resp.endswith("VALID"):
            raise RuntimeError(f"Set operation failed: {cmd!r}. Reply: {resp!r}")

    def _get_val(self, cmd: str, *, strict: bool = True) -> float:
        resp = self.ask(cmd)
        self._last_resp = resp

        # normalize common unicode variants
        resp = resp.replace("μ", "µ")

        if resp.endswith("N/A"):
            raise ValueError(
                "A wrong board id was used. Use READ:SYS:CAT to get valid device ids."
            )
        if resp.endswith("INVALID"):
            raise RuntimeError(f"Mercury iTC rejected query: {cmd!r}. Reply: {resp!r}")

        try:
            return self._parse_mercury_float(resp)
        except Exception:
            if strict:
                raise
            return float("nan")

    def _get_on_off(self, cmd: str) -> str:
        resp = self.ask(cmd)
        self._last_resp = resp

        if resp.endswith("N/A"):
            raise ValueError(
                "A wrong board id was used. Use READ:SYS:CAT to get valid device ids."
            )
        if resp.endswith("INVALID"):
            raise RuntimeError(f"Mercury iTC rejected query: {cmd!r}. Reply: {resp!r}")

        tokens = [tok.strip().upper() for tok in resp.strip().split(":")]
        for tok in reversed(tokens):
            if tok in ("ON", "OFF"):
                return tok

        raise ValueError(f"Mercury iTC returned no ON/OFF token: {resp!r}")

    def _parse_mercury_float(self, resp: str) -> float:
        tokens = [tok.strip() for tok in resp.strip().split(":")]

        for tok in reversed(tokens):
            cleaned = self._strip_unit_suffix(tok)
            if self._NUMERIC_RE.fullmatch(cleaned):
                return float(cleaned)

        raise ValueError(f"Mercury iTC returned an invalid numeric string: {resp!r}")

    @staticmethod
    def _strip_unit_suffix(token: str) -> str:
        s = token.strip()
        for suffix in (
            "µW",
            "uW",
            "mW",
            "W",
            "K",
            "%",
            "V",
            "mB",
            "MB",
            "bar",
            "mbar",
        ):
            if s.endswith(suffix):
                s = s[: -len(suffix)].strip()
                break
        return s

    def _get_last_response(self) -> str:
        return self._last_resp

    # ------------------------
    # Convenience methods
    # ------------------------

    def read_sys_cat(self) -> str:
        resp = self.ask("READ:SYS:CAT")
        self._last_resp = resp
        return resp

    def get_temperature(self, sensor_dev: str) -> float:
        return self._get_val(f"READ:DEV:{sensor_dev}:TEMP:SIG:TEMP")

    def get_temperature_setpoint(self, loop_dev: str) -> float:
        return self._get_val(f"READ:DEV:{loop_dev}:TEMP:LOOP:TSET")

    def set_temperature(self, temperature_K: float, loop_dev: str) -> None:
        self._set_val(f"SET:DEV:{loop_dev}:TEMP:LOOP:TSET:{temperature_K}")

    def get_heater_auto(self, loop_dev: str) -> str:
        return self._get_on_off(f"READ:DEV:{loop_dev}:TEMP:LOOP:ENAB")

    def set_heater_auto(self, loop_dev: str, auto_on: bool) -> None:
        state = "ON" if auto_on else "OFF"
        self._set_val(f"SET:DEV:{loop_dev}:TEMP:LOOP:ENAB:{state}")

    def get_heater_output(self, heater_dev: str) -> float:
        return self._get_val(f"READ:DEV:{heater_dev}:HTR:SIG:POWR")

    def get_pressure(self, pressure_dev: Optional[str] = None) -> float:
        dev = pressure_dev or self._pressure_dev
        return self._get_val(f"READ:DEV:{dev}:PRES:SIG:PRES")

    def get_pressure_setpoint(self, pressure_dev: Optional[str] = None) -> float:
        dev = pressure_dev or self._pressure_dev
        return self._get_val(f"READ:DEV:{dev}:PRES:LOOP:PRST")

    def set_pressure(self, pressure_mB: float, pressure_dev: Optional[str] = None) -> None:
        dev = pressure_dev or self._pressure_dev
        self._set_val(f"SET:DEV:{dev}:PRES:LOOP:PRST:{pressure_mB}")

    def get_pressure_auto(self, pressure_dev: Optional[str] = None) -> str:
        dev = pressure_dev or self._pressure_dev
        return self._get_on_off(f"READ:DEV:{dev}:PRES:LOOP:FAUT")

    def set_pressure_auto(self, auto_on: bool, pressure_dev: Optional[str] = None) -> None:
        dev = pressure_dev or self._pressure_dev
        state = "ON" if auto_on else "OFF"
        self._set_val(f"SET:DEV:{dev}:PRES:LOOP:FAUT:{state}")

    def get_needle_valve(self, valve_dev: Optional[str] = None) -> float:
        dev = valve_dev or self._valve_dev
        return self._get_val(f"READ:DEV:{dev}:AUX:SIG:PERC")


class TeslatronGUI:
    """
    Simple ipywidgets GUI for an existing MercuryiTC instance.
    """

    def __init__(self, itc: MercuryiTC):
        self.itc = itc
        self.name = itc.name
        self.address = getattr(itc, "address", "unknown")
        print(f"✅ Teslatron GUI attached to {self.name} @ {self.address}")

        self.build_ui()
        self.update_monitor()

    def build_ui(self):
        self.lbl_status = widgets.Label("Status: Ready")
        self.out_monitor = widgets.HTML(value="<b>No data fetched yet.</b>")
        self.btn_refresh = widgets.Button(description="Refresh", icon="refresh")
        self.btn_refresh.on_click(self.update_monitor)

        self.txt_sample_set = widgets.FloatText(value=10.0, description="Sample T (K):")
        self.dd_sample_auto = widgets.Dropdown(
            options=["Manual", "Auto"], value="Auto", description="Sample loop:"
        )
        self.btn_sample_apply = widgets.Button(
            description="Apply Sample", button_style="warning"
        )
        self.btn_sample_apply.on_click(self.on_set_sample)

        self.txt_vti_set = widgets.FloatText(value=20.0, description="VTI T (K):")
        self.dd_vti_auto = widgets.Dropdown(
            options=["Manual", "Auto"], value="Auto", description="VTI loop:"
        )
        self.btn_vti_apply = widgets.Button(
            description="Apply VTI", button_style="info"
        )
        self.btn_vti_apply.on_click(self.on_set_vti)

        self.txt_pressure_set = widgets.FloatText(value=10.0, description="Pressure (mB):")
        self.dd_pressure_auto = widgets.Dropdown(
            options=["Manual", "Auto"], value="Auto", description="Pressure:"
        )
        self.btn_pressure_apply = widgets.Button(
            description="Apply Pressure", button_style="success"
        )
        self.btn_pressure_apply.on_click(self.on_set_pressure)

        ui = widgets.VBox([
            widgets.HTML("<h3>Teslatron iTC Control</h3>"),
            widgets.HBox([self.txt_sample_set, self.dd_sample_auto, self.btn_sample_apply]),
            widgets.HBox([self.txt_vti_set, self.dd_vti_auto, self.btn_vti_apply]),
            widgets.HBox([self.txt_pressure_set, self.dd_pressure_auto, self.btn_pressure_apply]),
            widgets.HTML("<hr>"),
            self.btn_refresh,
            self.out_monitor,
            self.lbl_status,
        ])

        display(ui)

    def safe_get(self, func, fallback="—"):
        try:
            val = func()
            if isinstance(val, float):
                return f"{val:.4f}"
            return str(val)
        except Exception as e:
            return f"ERR: {e}"

    def update_monitor(self, b=None):
        self.lbl_status.value = "Status: Fetching..."
        try:
            sample_T = self.safe_get(self.itc.sample_temperature)
            sample_Tset = self.safe_get(self.itc.sample_temperature_setpoint)
            sample_auto = self.safe_get(self.itc.sample_heater_auto)

            vti_T = self.safe_get(self.itc.vti_temperature)
            vti_Tset = self.safe_get(self.itc.vti_temperature_setpoint)
            vti_auto = self.safe_get(self.itc.vti_heater_auto)

            pressure = self.safe_get(self.itc.pressure)
            pressure_set = self.safe_get(self.itc.pressure_setpoint)
            pressure_auto = self.safe_get(self.itc.pressure_auto)

            valve = self.safe_get(self.itc.needle_valve)
            sample_power = self.safe_get(self.itc.sample_heater_power)
            vti_power = self.safe_get(self.itc.vti_heater_power)

            html = f"""
            <table style="width:100%; border-collapse:collapse; font-family:sans-serif;">
              <tr style="background:#f2f2f2;">
                <th style="border:1px solid #ccc; padding:4px;">Channel</th>
                <th style="border:1px solid #ccc; padding:4px;">Value</th>
                <th style="border:1px solid #ccc; padding:4px;">Setpoint</th>
                <th style="border:1px solid #ccc; padding:4px;">Mode</th>
                <th style="border:1px solid #ccc; padding:4px;">Unit</th>
              </tr>
              <tr>
                <td style="border:1px solid #ccc; padding:4px;">Sample Temp</td>
                <td style="border:1px solid #ccc; padding:4px;">{sample_T}</td>
                <td style="border:1px solid #ccc; padding:4px;">{sample_Tset}</td>
                <td style="border:1px solid #ccc; padding:4px;">{sample_auto}</td>
                <td style="border:1px solid #ccc; padding:4px;">K</td>
              </tr>
              <tr>
                <td style="border:1px solid #ccc; padding:4px;">VTI Temp</td>
                <td style="border:1px solid #ccc; padding:4px;">{vti_T}</td>
                <td style="border:1px solid #ccc; padding:4px;">{vti_Tset}</td>
                <td style="border:1px solid #ccc; padding:4px;">{vti_auto}</td>
                <td style="border:1px solid #ccc; padding:4px;">K</td>
              </tr>
              <tr>
                <td style="border:1px solid #ccc; padding:4px;">Pressure</td>
                <td style="border:1px solid #ccc; padding:4px;">{pressure}</td>
                <td style="border:1px solid #ccc; padding:4px;">{pressure_set}</td>
                <td style="border:1px solid #ccc; padding:4px;">{pressure_auto}</td>
                <td style="border:1px solid #ccc; padding:4px;">mB</td>
              </tr>
              <tr>
                <td style="border:1px solid #ccc; padding:4px;">Needle Valve</td>
                <td style="border:1px solid #ccc; padding:4px;">{valve}</td>
                <td style="border:1px solid #ccc; padding:4px;">—</td>
                <td style="border:1px solid #ccc; padding:4px;">—</td>
                <td style="border:1px solid #ccc; padding:4px;">%</td>
              </tr>
              <tr>
                <td style="border:1px solid #ccc; padding:4px;">Sample Heater Power</td>
                <td style="border:1px solid #ccc; padding:4px;">{sample_power}</td>
                <td style="border:1px solid #ccc; padding:4px;">—</td>
                <td style="border:1px solid #ccc; padding:4px;">—</td>
                <td style="border:1px solid #ccc; padding:4px;">W</td>
              </tr>
              <tr>
                <td style="border:1px solid #ccc; padding:4px;">VTI Heater Power</td>
                <td style="border:1px solid #ccc; padding:4px;">{vti_power}</td>
                <td style="border:1px solid #ccc; padding:4px;">—</td>
                <td style="border:1px solid #ccc; padding:4px;">—</td>
                <td style="border:1px solid #ccc; padding:4px;">W</td>
              </tr>
            </table>
            """
            self.out_monitor.value = html
            self.lbl_status.value = "Status: Updated."
        except Exception as e:
            self.lbl_status.value = f"Status: Error while updating: {e}"

    def on_set_sample(self, b):
        try:
            self.itc.sample_heater_auto(self.dd_sample_auto.value)
            self.itc.sample_temperature_setpoint(self.txt_sample_set.value)
            self.lbl_status.value = "Status: Sample settings applied."
            self.update_monitor()
        except Exception as e:
            self.lbl_status.value = f"Status: Sample set failed: {e}"

    def on_set_vti(self, b):
        try:
            self.itc.vti_heater_auto(self.dd_vti_auto.value)
            self.itc.vti_temperature_setpoint(self.txt_vti_set.value)
            self.lbl_status.value = "Status: VTI settings applied."
            self.update_monitor()
        except Exception as e:
            self.lbl_status.value = f"Status: VTI set failed: {e}"

    def on_set_pressure(self, b):
        try:
            self.itc.pressure_auto(self.dd_pressure_auto.value)
            self.itc.pressure_setpoint(self.txt_pressure_set.value)
            self.lbl_status.value = "Status: Pressure settings applied."
            self.update_monitor()
        except Exception as e:
            self.lbl_status.value = f"Status: Pressure set failed: {e}"