"""
Reduction Experiment Controller
---------------------------------
Combines Watlow EZ-Zone temperature control and MFC flow control
into a single automated experiment script.

Stages:
  RAMP  : 25°C → 600°C at 10°C/min
  HOLD1 : 600°C for 60 min  (H2 reduction flow)
  HOLD2 : 600°C for 30 min  (purge flow)
  COOL  : Control loop OFF, purge flow continues

Safety: If AI2 > 85°C or any error/interrupt → safe flow config + heater off

Usage:
    python reduction_experiment.py
"""

import struct
import csv
import time
from datetime import datetime
from binascii import unhexlify

import serial
import crcmod

# ═══════════════════════════════════════════════════════════════
#  KEY PARAMETERS — edit these as needed
# ═══════════════════════════════════════════════════════════════

# Ports
WATLOW_PORT     = 'COM4'
MFC_PORT        = 'COM2'

# Temperature
TARGET_TEMP     = 600.0    # °C  — ramp target
RAMP_RATE       = 10.0     # °C/min
AI2_SAFETY_LIMIT = 85.0    # °C  — AI2 shutoff threshold

# Hold durations
HOLD1_DURATION  = 60       # minutes — first hold (reduction)
HOLD2_DURATION  = 30       # minutes — second hold (purge)

# Logging
LOG_INTERVAL    = 5 * 60   # seconds (5 minutes)

# MFC flow configurations  {channel: flow_mL_min}
FLOW_RAMP = {1: 0,  2: 0,  3: 20, 4: 4}   # during ramp
FLOW_HOLD1 = {1: 0, 2: 8,  3: 14, 4: 2}   # first 60 min hold
FLOW_HOLD2 = {1: 0, 2: 0,  3: 20, 4: 4}   # last 30 min hold
FLOW_SAFE  = {1: 0, 2: 0,  3: 20, 4: 4}   # safety / cooldown

# MFC serial settings
MFC_BAUD    = 9600
MFC_PORT_MAP = {1: '02', 2: '04', 3: '06', 4: '08'}

# ═══════════════════════════════════════════════════════════════


# ── Watlow Controller ────────────────────────────────────────
crc8  = crcmod.mkCrcFun(0b110000001)
crc16 = crcmod.mkCrcFun(0b10001000000100001)

def f_to_c(f):
    return (f - 32.0) / 1.8

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
        """Set a channel flow rate in mL/min. Use 0 to close."""
        port = MFC_PORT_MAP[channel]
        self._send(f'AZ.{port}P46=1')
        self._send(f'AZ.{port}P01={flow:.2f}')
        label = "CLOSED" if flow == 0 else f"{flow} mL/min"
        print(f"  [MFC] CH{channel} → {label}")

    def set_flows(self, config, label=''):
        """Apply a full flow configuration dict."""
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
        flows.get(1, '-'), flows.get(2, '-'),
        flows.get(3, '-'), flows.get(4, '-'),
        stage
    ])
    csvfile.flush()


# ── Main Experiment ──────────────────────────────────────────
def run_experiment():
    tc  = WatlowController(WATLOW_PORT)
    mfc = MFCController(MFC_PORT, MFC_BAUD)

    filename = f"reduction_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    current_flows = FLOW_SAFE.copy()
    stage = "INIT"

    print()
    print("=" * 62)
    print("  REDUCTION EXPERIMENT")
    print("=" * 62)
    print(f"  Watlow port   : {WATLOW_PORT}")
    print(f"  MFC port      : {MFC_PORT}")
    print(f"  Target temp   : {TARGET_TEMP} °C")
    print(f"  Ramp rate     : {RAMP_RATE} °C/min")
    print(f"  AI2 limit     : {AI2_SAFETY_LIMIT} °C")
    print(f"  Hold 1        : {HOLD1_DURATION} min  (reduction flow)")
    print(f"  Hold 2        : {HOLD2_DURATION} min  (purge flow)")
    print(f"  Log interval  : every {LOG_INTERVAL//60} min")
    print(f"  CSV file      : {filename}")
    print("=" * 62)
    print("  Press Ctrl+C to stop safely at any time")
    print("=" * 62)
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
            print("  [1/4] Starting up...")

            # Test connection before doing anything
            print("  Checking Watlow connection...")
            try:
                test = tc.get()
                print(f"  Watlow connected. Current temp: {test['actual']:.2f} °C")
            except OSError:
                raise RuntimeError(
                    f"Cannot communicate with Watlow on {WATLOW_PORT}. "
                    "Make sure: (1) proprietary software is closed, "
                    "(2) no other script is using the port, "
                    "(3) USB-RS485 adapter is plugged in."
                )

            tc.set_control_loop('on')
            time.sleep(1)

            start_temp = tc.get()['actual']
            experiment_start = time.time()
            last_log_time = 0

            print(f"  Starting temperature: {start_temp:.2f} °C")
            print()

            # ── STAGE 1: RAMP ─────────────────────────────────
            print("=" * 62)
            print("  STAGE 1: RAMP — setting MFC flows for ramp")
            print("=" * 62)
            stage = "RAMP"
            current_flows = FLOW_RAMP.copy()
            mfc.set_flows(current_flows, "Ramp flows")
            print()
            print(f"  {'Time(min)':>10} {'Setpoint':>10} {'AI1 Bed':>10} {'AI2 Surf':>10}  Stage")
            print(f"  {'-'*10} {'-'*10} {'-'*10} {'-'*10}  -----")

            ramp_reached = False
            while not ramp_reached:
                elapsed_min = (time.time() - experiment_start) / 60.0
                ramp_setpoint = min(start_temp + RAMP_RATE * elapsed_min, TARGET_TEMP)

                try:
                    tc.set(ramp_setpoint)
                    ai1, ai2 = tc.read_temperatures()
                except OSError:
                    print(f"  ! Watlow connection error — retrying in 5s...")
                    time.sleep(5)
                    continue

                # AI2 safety check
                if ai2 is not None and ai2 > AI2_SAFETY_LIMIT:
                    raise RuntimeError(f"AI2 safety limit exceeded: {ai2:.2f}°C > {AI2_SAFETY_LIMIT}°C")

                # Log every LOG_INTERVAL seconds
                now = time.time()
                if now - last_log_time >= LOG_INTERVAL:
                    print(f"  {elapsed_min:>10.2f} {ramp_setpoint:>9.2f}° "
                          f"{ai1:>9.2f}°  {ai2:>9.2f}°  {stage}")
                    log_row(writer, csvfile, elapsed_min, ramp_setpoint,
                            ai1, ai2, current_flows, stage)
                    last_log_time = now

                if ramp_setpoint >= TARGET_TEMP:
                    ramp_reached = True
                    print()
                    print(f"  ✓ Reached {TARGET_TEMP}°C")

                time.sleep(5)

            # ── STAGE 2: HOLD 1 (60 min, reduction flow) ─────
            print()
            print("=" * 62)
            print(f"  STAGE 2: HOLD 1 — {HOLD1_DURATION} min at {TARGET_TEMP}°C (reduction flow)")
            print("=" * 62)
            stage = "HOLD1"
            current_flows = FLOW_HOLD1.copy()
            mfc.set_flows(current_flows, "Reduction flows")
            print()

            hold1_start = time.time()
            while (time.time() - hold1_start) < HOLD1_DURATION * 60:
                elapsed_min = (time.time() - experiment_start) / 60.0
                try:
                    tc.set(TARGET_TEMP)
                    ai1, ai2 = tc.read_temperatures()
                except OSError:
                    print(f"  ! Watlow connection error — retrying in 5s...")
                    time.sleep(5)
                    continue

                if ai2 is not None and ai2 > AI2_SAFETY_LIMIT:
                    raise RuntimeError(f"AI2 safety limit exceeded: {ai2:.2f}°C > {AI2_SAFETY_LIMIT}°C")

                now = time.time()
                if now - last_log_time >= LOG_INTERVAL:
                    remaining = HOLD1_DURATION - (time.time() - hold1_start) / 60.0
                    print(f"  {elapsed_min:>10.2f} {TARGET_TEMP:>9.2f}° "
                          f"{ai1:>9.2f}°  {ai2:>9.2f}°  {stage}  ({remaining:.1f} min left)")
                    log_row(writer, csvfile, elapsed_min, TARGET_TEMP,
                            ai1, ai2, current_flows, stage)
                    last_log_time = now

                time.sleep(5)

            # ── STAGE 3: HOLD 2 (30 min, purge flow) ─────────
            print()
            print("=" * 62)
            print(f"  STAGE 3: HOLD 2 — {HOLD2_DURATION} min at {TARGET_TEMP}°C (purge flow)")
            print("=" * 62)
            stage = "HOLD2"
            current_flows = FLOW_HOLD2.copy()
            mfc.set_flows(current_flows, "Purge flows")
            print()

            hold2_start = time.time()
            while (time.time() - hold2_start) < HOLD2_DURATION * 60:
                elapsed_min = (time.time() - experiment_start) / 60.0
                try:
                    tc.set(TARGET_TEMP)
                    ai1, ai2 = tc.read_temperatures()
                except OSError:
                    print(f"  ! Watlow connection error — retrying in 5s...")
                    time.sleep(5)
                    continue

                if ai2 is not None and ai2 > AI2_SAFETY_LIMIT:
                    raise RuntimeError(f"AI2 safety limit exceeded: {ai2:.2f}°C > {AI2_SAFETY_LIMIT}°C")

                now = time.time()
                if now - last_log_time >= LOG_INTERVAL:
                    remaining = HOLD2_DURATION - (time.time() - hold2_start) / 60.0
                    print(f"  {elapsed_min:>10.2f} {TARGET_TEMP:>9.2f}° "
                          f"{ai1:>9.2f}°  {ai2:>9.2f}°  {stage}  ({remaining:.1f} min left)")
                    log_row(writer, csvfile, elapsed_min, TARGET_TEMP,
                            ai1, ai2, current_flows, stage)
                    last_log_time = now

                time.sleep(5)

            # ── STAGE 4: COOLDOWN ─────────────────────────────
            print()
            print("=" * 62)
            print("  STAGE 4: COOLDOWN — control loop OFF, purge flow")
            print("=" * 62)
            stage = "COOL"
            tc.set(25.0)
            tc.set_control_loop('off')
            current_flows = FLOW_SAFE.copy()
            mfc.set_flows(current_flows, "Cooldown/safe flows")
            print()
            print("  Experiment complete! Logging cooldown — press Ctrl+C to stop.")
            print()

            # Keep logging during cooldown until user stops
            while True:
                elapsed_min = (time.time() - experiment_start) / 60.0
                try:
                    ai1, ai2 = tc.read_temperatures()
                except OSError:
                    ai1, ai2 = None, None

                now = time.time()
                if now - last_log_time >= LOG_INTERVAL:
                    print(f"  {elapsed_min:>10.2f} {'OFF':>10} "
                          f"{ai1:>9.2f}°  {ai2:>9.2f}°  {stage}")
                    log_row(writer, csvfile, elapsed_min, 0,
                            ai1, ai2, current_flows, stage)
                    last_log_time = now

                time.sleep(5)

    except RuntimeError as e:
        print()
        print(f"  !! SAFETY SHUTOFF: {e}")
        stage = "SHUTOFF"

    except KeyboardInterrupt:
        print()
        print("  Stopped by user.")

    finally:
        # ── Safe shutdown ─────────────────────────────────────
        print()
        print("  Running safe shutdown...")
        try:
            current_flows = FLOW_SAFE.copy()
            mfc.set_flows(current_flows, "Safe flows")
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
