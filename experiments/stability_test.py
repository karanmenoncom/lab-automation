"""
Stability Test Experiment Controller
--------------------------------------
Stages:
  RAMP  : Current → 375°C at 10°C/min  CH3=20, CH4=4
  HOLD  : 375°C for 3 hours             CH1=20, CH4=4
  COOL  : Heater OFF for 30 min         CH1=20, CH4=4
  END   : Final flow until Ctrl+C       CH3=20, CH4=4

Safety: If AI2 > safety limit or any error/interrupt → safe flows + heater off

Usage:
    python stability_test.py
"""

import struct
import csv
import time
from datetime import datetime
from binascii import unhexlify

import serial
import crcmod
from heartbeat import write_heartbeat, clear_heartbeat

# ═══════════════════════════════════════════════════════════════
#  KEY PARAMETERS — edit these as needed
# ═══════════════════════════════════════════════════════════════

# Ports
WATLOW_PORT      = 'COM4'
MFC_PORT         = 'COM2'

# Temperature
TARGET_TEMP      = 375.0   # °C — ramp target
RAMP_RATE        = 10.0    # °C/min
AI2_SAFETY_LIMIT = 85.0    # °C — AI2 shutoff threshold

# Hold durations
HOLD_DURATION    = 3 * 60  # minutes — hold at temp
COOL_DURATION    = 30      # minutes — post-heater-off purge

# Logging
LOG_INTERVAL     = 5 * 60  # seconds (5 minutes)

# MFC flow configurations {channel: flow_mL_min}
FLOW_RAMP  = {1: 0,  2: 0, 3: 20, 4: 4}   # during ramp
FLOW_HOLD  = {1: 20, 2: 0, 3: 0,  4: 4}   # hold + cool purge
FLOW_END   = {1: 0,  2: 0, 3: 20, 4: 4}   # final flow
FLOW_SAFE  = {1: 0,  2: 0, 3: 20, 4: 4}   # emergency safe flow

# MFC serial settings
MFC_BAUD     = 9600
MFC_PORT_MAP = {1: '02', 2: '04', 3: '06', 4: '08'}

# ═══════════════════════════════════════════════════════════════


# ── CRC functions ────────────────────────────────────────────
crc8  = crcmod.mkCrcFun(0b110000001)
crc16 = crcmod.mkCrcFun(0b10001000000100001)

def f_to_c(f):
    return (f - 32.0) / 1.8


# ── Watlow Controller ────────────────────────────────────────
class WatlowController:
    """Direct Watlow driver — no dependency on numat/watlow package."""

    def __init__(self, port, timeout=1.0):
        self.connection = serial.Serial(port, 38400, timeout=timeout)
        self.connection.reset_input_buffer()
        self.connection.reset_output_buffer()
        time.sleep(0.2)

    def _send(self, header_hex, body_hex, read_bytes=21):
        preamble = unhexlify('55ff')
        header   = unhexlify(header_hex)
        body     = unhexlify(body_hex)
        hcrc = struct.pack('<H', ~crc8(header) & 0xff)
        dcrc = struct.pack('<H', ~crc16(body) & 0xffff)
        self.connection.reset_input_buffer()
        self.connection.write(preamble + header + hcrc[:1] + body + dcrc)
        return bytes.hex(self.connection.read(read_bytes))

    def get(self):
        actual_hex   = self._send('0510000006', '010301040101')
        setpoint_hex = self._send('0510000006', '010301070101')
        actual   = f_to_c(struct.unpack('>f', unhexlify(actual_hex[30:38]))[0])
        setpoint = f_to_c(struct.unpack('>f', unhexlify(setpoint_hex[30:38]))[0])
        return {'actual': actual, 'setpoint': setpoint}

    def get_analog_input_2(self):
        """Read AI2 surface thermocouple, returns °C."""
        resp = self._send('0510000006', '010301040102')
        return f_to_c(struct.unpack('>f', unhexlify(resp[30:38]))[0])

    def set(self, setpoint_c):
        """Set the temperature setpoint in °C."""
        setpoint_f = setpoint_c * 1.8 + 32.0
        body = unhexlify('010407010108') + struct.pack('>f', setpoint_f)
        header = unhexlify('051000000a')
        hcrc = struct.pack('<H', ~crc8(header) & 0xff)
        dcrc = struct.pack('<H', ~crc16(body) & 0xffff)
        self.connection.reset_input_buffer()
        self.connection.write(unhexlify('55ff') + header + hcrc[:1] + body + dcrc)
        self.connection.read(20)

    def set_control_loop(self, mode):
        """Turn control loop on or off. mode: 'on' or 'off'"""
        value = '000a' if mode == 'on' else '003e'
        self._send('0510030009', f'01040801010f01{value}')
        print(f"  [Watlow] Control loop → {mode.upper()}")

    def read_temperatures(self):
        """Read AI1 and AI2. Returns (ai1, ai2) or (None, None) on error."""
        try:
            data = self.get()
            ai2  = self.get_analog_input_2()
            return data['actual'], ai2
        except Exception:
            return None, None

    def close(self):
        self.connection.close()


# ── MFC Controller ───────────────────────────────────────────
class MFCController:
    """Simple MFC controller over serial."""

    def __init__(self, port, baud=9600):
        self.ser = serial.Serial(
            port=port, baudrate=baud,
            bytesize=8, parity='N',
            stopbits=1, timeout=2
        )

    def _send(self, command):
        self.ser.write((command + '\r').encode())
        time.sleep(0.1)
        return self.ser.readline().decode().strip()

    def set_channel(self, channel, flow):
        port = MFC_PORT_MAP[channel]
        self._send(f'AZ.{port}P46=1')
        self._send(f'AZ.{port}P01={flow:.2f}')
        label = "CLOSED" if flow == 0 else f"{flow} mL/min"
        print(f"  [MFC] CH{channel} → {label}")

    def set_flows(self, config, label=''):
        if label:
            print(f"  [MFC] Setting flows: {label}")
        for ch, flow in config.items():
            self.set_channel(ch, flow)

    def close(self):
        self.ser.close()


# ── Logging ──────────────────────────────────────────────────
def log_row(writer, csvfile, elapsed_min, setpoint, ai1, ai2, flows, stage):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    writer.writerow([
        ts, f"{elapsed_min:.2f}", f"{setpoint:.2f}",
        f"{ai1:.2f}" if ai1 is not None else "ERR",
        f"{ai2:.2f}" if ai2 is not None else "ERR",
        flows.get(1, 0), flows.get(2, 0),
        flows.get(3, 0), flows.get(4, 0),
        stage
    ])
    csvfile.flush()


def print_row(elapsed_min, setpoint, ai1, ai2, stage, extra=''):
    sp = f"{setpoint:.2f}°" if setpoint > 0 else "OFF"
    a1 = f"{ai1:.2f}°" if ai1 is not None else "ERR"
    a2 = f"{ai2:.2f}°" if ai2 is not None else "ERR"
    print(f"  {elapsed_min:>10.2f} {sp:>10} {a1:>10} {a2:>10}  [{stage}]  {extra}")


# ── Stage runner ─────────────────────────────────────────────
def run_timed_stage(tc, mfc, writer, csvfile, stage_name, duration_min,
                    setpoint, flows, experiment_start, last_log_time,
                    ai2_limit, log_interval):
    """
    Run a fixed-duration hold stage.
    Returns updated last_log_time.
    Raises RuntimeError on AI2 safety trip.
    """
    stage_start = time.time()
    while (time.time() - stage_start) < duration_min * 60:
        elapsed_min = (time.time() - experiment_start) / 60.0
        remaining   = duration_min - (time.time() - stage_start) / 60.0

        try:
            tc.set(setpoint)
            ai1, ai2 = tc.read_temperatures()
        except OSError:
            print(f"  ! Watlow connection error — retrying in 5s...")
            write_heartbeat()
            time.sleep(5)
            continue

        if ai2 is not None and ai2 > ai2_limit:
            raise RuntimeError(f"AI2 safety limit exceeded: {ai2:.2f}°C > {ai2_limit}°C")

        now = time.time()
        if now - last_log_time >= log_interval:
            print_row(elapsed_min, setpoint, ai1, ai2, stage_name,
                      f"({remaining:.1f} min left)")
            log_row(writer, csvfile, elapsed_min, setpoint,
                    ai1, ai2, flows, stage_name)
            last_log_time = now

        write_heartbeat()
        time.sleep(5)

    return last_log_time


# ── Main Experiment ──────────────────────────────────────────
def run_experiment():
    tc  = WatlowController(WATLOW_PORT)
    mfc = MFCController(MFC_PORT, MFC_BAUD)

    filename = f"stability_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    current_flows = FLOW_SAFE.copy()
    stage = "INIT"

    print()
    print("=" * 65)
    print("  STABILITY TEST EXPERIMENT")
    print("=" * 65)
    print(f"  Watlow port    : {WATLOW_PORT}")
    print(f"  MFC port       : {MFC_PORT}")
    print(f"  Target temp    : {TARGET_TEMP} °C")
    print(f"  Ramp rate      : {RAMP_RATE} °C/min")
    print(f"  AI2 limit      : {AI2_SAFETY_LIMIT} °C")
    print(f"  Hold 1         : {HOLD1_DURATION} min  (ramp flows)")
    print(f"  Hold 2         : {HOLD2_DURATION} min  (reaction flows)")
    print(f"  Cool purge     : {COOL_DURATION} min  (post-heater)")
    print(f"  Log interval   : every {LOG_INTERVAL//60} min")
    print(f"  CSV file       : {filename}")
    print("=" * 65)
    print("  Press Ctrl+C to stop safely at any time")
    print("=" * 65)
    print()

    try:
        with open(filename, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow([
                'timestamp', 'elapsed_min', 'setpoint_C',
                'AI1_bed_C', 'AI2_surface_C',
                'CH1_mL_min', 'CH2_mL_min', 'CH3_mL_min', 'CH4_mL_min',
                'stage'
            ])

            # ── Startup ──────────────────────────────────────
            print("  Checking Watlow connection...")
            try:
                test = tc.get()
                print(f"  Connected. Current temp: {test['actual']:.2f}°C  "
                      f"Setpoint: {test['setpoint']:.2f}°C")
            except Exception:
                raise RuntimeError(
                    f"Cannot communicate with Watlow on {WATLOW_PORT}. "
                    "Make sure no other software is using the port."
                )

            # Reset setpoint to 25°C before starting
            print("  Resetting setpoint to 25°C...")
            tc.set(25.0)
            time.sleep(0.5)

            tc.set_control_loop('on')
            write_heartbeat()
            time.sleep(1)

            start_temp = tc.get()['actual']
            experiment_start = time.time()
            last_log_time = 0

            print(f"  Starting temperature: {start_temp:.2f}°C")
            print()
            print(f"  {'Time(min)':>10} {'Setpoint':>10} {'AI1 Bed':>10} "
                  f"{'AI2 Surf':>10}  Stage")
            print(f"  {'-'*10} {'-'*10} {'-'*10} {'-'*10}  -----")

            # ── STAGE 1: RAMP ─────────────────────────────────
            print()
            print("=" * 65)
            print(f"  STAGE 1: RAMP — {start_temp:.1f}°C → {TARGET_TEMP}°C "
                  f"at {RAMP_RATE}°C/min")
            print("=" * 65)
            stage = "RAMP"
            current_flows = FLOW_RAMP.copy()
            mfc.set_flows(current_flows, "Ramp flows")
            print()

            ramp_reached = False
            while not ramp_reached:
                elapsed_min   = (time.time() - experiment_start) / 60.0
                ramp_setpoint = min(start_temp + RAMP_RATE * elapsed_min, TARGET_TEMP)

                try:
                    tc.set(ramp_setpoint)
                    ai1, ai2 = tc.read_temperatures()
                except OSError:
                    print("  ! Watlow connection error — retrying in 5s...")
                    write_heartbeat()
                    time.sleep(5)
                    continue

                if ai2 is not None and ai2 > AI2_SAFETY_LIMIT:
                    raise RuntimeError(
                        f"AI2 safety limit exceeded: {ai2:.2f}°C > {AI2_SAFETY_LIMIT}°C"
                    )

                now = time.time()
                if now - last_log_time >= LOG_INTERVAL:
                    print_row(elapsed_min, ramp_setpoint, ai1, ai2, stage)
                    log_row(writer, csvfile, elapsed_min, ramp_setpoint,
                            ai1, ai2, current_flows, stage)
                    last_log_time = now

                if ramp_setpoint >= TARGET_TEMP:
                    ramp_reached = True
                    print()
                    print(f"  ✓ Reached {TARGET_TEMP}°C")

                write_heartbeat()
                time.sleep(5)

            # ── STAGE 2: HOLD (3 hours, CH1=20 CH4=4) ────────
            print()
            print("=" * 65)
            print(f"  STAGE 2: HOLD — {HOLD_DURATION} min at {TARGET_TEMP}°C")
            print("=" * 65)
            stage = "HOLD"
            current_flows = FLOW_HOLD.copy()
            mfc.set_flows(current_flows, "Hold flows")
            print()

            last_log_time = run_timed_stage(
                tc, mfc, writer, csvfile, stage,
                HOLD_DURATION, TARGET_TEMP, current_flows,
                experiment_start, last_log_time,
                AI2_SAFETY_LIMIT, LOG_INTERVAL
            )

            # ── STAGE 4: HEATER OFF, COOL PURGE (30 min) ─────
            print()
            print("=" * 65)
            print(f"  STAGE 3: COOL PURGE — heater OFF, {COOL_DURATION} min "
                  f"(CH1=20, CH4=4 maintained)")
            print("=" * 65)
            stage = "COOL"
            tc.set(25.0)
            tc.set_control_loop('off')
            current_flows = FLOW_HOLD.copy()
            mfc.set_flows(current_flows, "Cool purge flows (maintained)")
            print()

            cool_start = time.time()
            while (time.time() - cool_start) < COOL_DURATION * 60:
                elapsed_min = (time.time() - experiment_start) / 60.0
                remaining   = COOL_DURATION - (time.time() - cool_start) / 60.0
                try:
                    ai1, ai2 = tc.read_temperatures()
                except OSError:
                    ai1, ai2 = None, None

                now = time.time()
                if now - last_log_time >= LOG_INTERVAL:
                    print_row(elapsed_min, 0, ai1, ai2, stage,
                              f"({remaining:.1f} min left)")
                    log_row(writer, csvfile, elapsed_min, 0,
                            ai1, ai2, current_flows, stage)
                    last_log_time = now

                write_heartbeat()
                time.sleep(5)

            # ── STAGE 5: END FLOW ─────────────────────────────
            print()
            print("=" * 65)
            print("  STAGE 4: END — switching to final flow config")
            print("=" * 65)
            stage = "END"
            current_flows = FLOW_END.copy()
            mfc.set_flows(current_flows, "Final flows")
            print()
            print("  Experiment complete! Press Ctrl+C to stop logging.")
            print()

            # Keep logging until user stops
            while True:
                elapsed_min = (time.time() - experiment_start) / 60.0
                try:
                    ai1, ai2 = tc.read_temperatures()
                except OSError:
                    ai1, ai2 = None, None

                now = time.time()
                if now - last_log_time >= LOG_INTERVAL:
                    print_row(elapsed_min, 0, ai1, ai2, stage)
                    log_row(writer, csvfile, elapsed_min, 0,
                            ai1, ai2, current_flows, stage)
                    last_log_time = now

                write_heartbeat()
                time.sleep(5)

    except RuntimeError as e:
        print()
        print(f"  !! SAFETY SHUTOFF: {e}")

    except KeyboardInterrupt:
        print()
        print("  Stopped by user.")

    finally:
        clear_heartbeat()
        print()
        print("  Running safe shutdown...")
        try:
            mfc.set_flows(FLOW_SAFE, "Safe flows")
        except Exception as e:
            print(f"  ! MFC error during shutdown: {e}")
        try:
            tc.set(25.0)
            tc.set_control_loop('off')
        except Exception as e:
            print(f"  ! Watlow error during shutdown: {e}")
        try:
            tc.close()
            mfc.close()
        except Exception:
            pass
        print(f"  Data saved to: {filename}")
        print("  Done.")


if __name__ == '__main__':
    run_experiment()
