"""
Full Kinetics Experiment
--------------------------
Stages:
  RAMP          : Current → 550°C at 10°C/min   CH3=24, CH4=0 (inert)
  HOLD          : 550°C indefinitely until kinetics start
  KINETIC_NH3   : Ammonia kinetics steps (5 steps x 30 min)
  PURGE1        : 15 min wait after NH3 kinetics  CH3=24 all others 0
  SWITCH        : Switch to CH3=24               CH1,2,4=0
  PURGE2        : 15 min wait before H2 kinetics
  KINETIC_H2    : Hydrogen kinetics steps (7 steps x 30 min)
  COOL          : Control loop OFF, 30 min purge  CH1=20, CH4=4
  END           : Final flow                      CH3=24, CH1,2,4=0

Safety: AI2 > limit or any error/interrupt → safe shutdown

Usage:
    python full_kinetics.py
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

WATLOW_PORT      = 'COM4'
MFC_PORT         = 'COM2'

TARGET_TEMP      = 550.0    # °C
RAMP_RATE        = 10.0     # °C/min
AI2_SAFETY_LIMIT = 85.0     # °C

LOG_INTERVAL     = 5 * 60   # seconds (5 minutes)

PURGE1_DURATION  = 15       # minutes — after NH3 kinetics
PURGE2_DURATION  = 15       # minutes — before H2 kinetics
COOL_DURATION    = 30       # minutes — post-heater-off

MFC_BAUD         = 9600
MFC_PORT_MAP     = {1: '02', 2: '04', 3: '06', 4: '08'}

# ── Flow configs ──────────────────────────────────────────────
FLOW_RAMP  = {1: 0,  2: 0, 3: 24, 4: 0}   # ramp / inert
FLOW_PURGE = {1: 0,  2: 0, 3: 24, 4: 0}   # purge between experiments
FLOW_COOL  = {1: 20, 2: 0, 3: 0,  4: 4}   # post-heater cooldown
FLOW_END   = {1: 0,  2: 0, 3: 24, 4: 0}   # final
FLOW_SAFE  = {1: 0,  2: 0, 3: 24, 4: 0}   # emergency

# ── NH3 Kinetics (from kinetic_ammonia.py) ────────────────────
NH3_STEP_MINUTES = 30
NH3_NUM_STEPS    = 5
NH3_START_CH1    = 20   # decreases by NH3_STEP_SIZE each step
NH3_START_CH4    = 4    # increases by NH3_STEP_SIZE each step
NH3_STEP_SIZE    = 2

# ── H2 Kinetics (from kinetic_hydrogen.py) ────────────────────
H2_STEP_MINUTES  = 30
# Each step: (channel_3_flow, channel_4_flow); CH1=20, CH2=0 always
H2_STEPS = [
    (0,   4),
    (1,   3),
    (1.5, 2.5),
    (2,   2),
    (2.5, 1.5),
    (3,   1),
    (0,   4),    # final reset
]

# ═══════════════════════════════════════════════════════════════


# ── CRC functions ─────────────────────────────────────────────
crc8  = crcmod.mkCrcFun(0b110000001)
crc16 = crcmod.mkCrcFun(0b10001000000100001)

def f_to_c(f):
    return (f - 32.0) / 1.8


# ── Watlow Controller ─────────────────────────────────────────
class WatlowController:

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
        resp = self._send('0510000006', '010301040102')
        return f_to_c(struct.unpack('>f', unhexlify(resp[30:38]))[0])

    def set(self, setpoint_c):
        setpoint_f = setpoint_c * 1.8 + 32.0
        body = unhexlify('010407010108') + struct.pack('>f', setpoint_f)
        header = unhexlify('051000000a')
        hcrc = struct.pack('<H', ~crc8(header) & 0xff)
        dcrc = struct.pack('<H', ~crc16(body) & 0xffff)
        self.connection.reset_input_buffer()
        self.connection.write(unhexlify('55ff') + header + hcrc[:1] + body + dcrc)
        self.connection.read(20)

    def set_control_loop(self, mode):
        value = '000a' if mode == 'on' else '003e'
        self._send('0510030009', f'01040801010f01{value}')
        print(f"  [Watlow] Control loop → {mode.upper()}")

    def read_temperatures(self):
        try:
            data = self.get()
            ai2  = self.get_analog_input_2()
            return data['actual'], ai2
        except Exception:
            return None, None

    def close(self):
        self.connection.close()


# ── MFC Controller ────────────────────────────────────────────
class MFCController:

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
            print(f"  [MFC] {label}")
        for ch, flow in config.items():
            self.set_channel(ch, flow)

    def close(self):
        self.ser.close()


# ── Logging helpers ───────────────────────────────────────────
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
    a1 = f"{ai1:.2f}°"      if ai1 is not None else "ERR"
    a2 = f"{ai2:.2f}°"      if ai2 is not None else "ERR"
    print(f"  {elapsed_min:>10.2f} {sp:>10} {a1:>10} {a2:>10}  [{stage}]  {extra}")


def check_ai2(ai2, limit, stage):
    if ai2 is not None and ai2 > limit:
        raise RuntimeError(
            f"[{stage}] AI2 safety limit exceeded: {ai2:.2f}°C > {limit}°C"
        )


# ── Stage runners ─────────────────────────────────────────────
def run_ramp(tc, writer, csvfile, from_temp, to_temp, rate,
             flows, experiment_start, last_log_time):
    """Ramp temperature. Returns last_log_time."""
    ramp_start = time.time()
    print(f"  Ramping {from_temp:.1f}°C → {to_temp:.1f}°C at {rate}°C/min")
    while True:
        elapsed_min  = (time.time() - experiment_start) / 60.0
        ramp_elapsed = (time.time() - ramp_start) / 60.0
        setpoint     = min(from_temp + rate * ramp_elapsed, to_temp)

        try:
            tc.set(setpoint)
            ai1, ai2 = tc.read_temperatures()
        except OSError:
            print("  ! Watlow error — retrying in 5s...")
            write_heartbeat()
            time.sleep(5)
            continue

        check_ai2(ai2, AI2_SAFETY_LIMIT, "RAMP")

        now = time.time()
        if now - last_log_time >= LOG_INTERVAL:
            print_row(elapsed_min, setpoint, ai1, ai2, "RAMP")
            log_row(writer, csvfile, elapsed_min, setpoint, ai1, ai2, flows, "RAMP")
            last_log_time = now

        if setpoint >= to_temp:
            print(f"\n  ✓ Reached {to_temp}°C")
            break

        write_heartbeat()
        time.sleep(5)

    return last_log_time


def run_timed_stage(tc, writer, csvfile, stage_name, duration_min,
                    setpoint, flows, experiment_start, last_log_time,
                    heater_on=True):
    """Hold or purge stage for a fixed duration. Returns last_log_time."""
    stage_start = time.time()
    while (time.time() - stage_start) < duration_min * 60:
        elapsed_min = (time.time() - experiment_start) / 60.0
        remaining   = duration_min - (time.time() - stage_start) / 60.0

        try:
            if heater_on:
                tc.set(setpoint)
            ai1, ai2 = tc.read_temperatures()
        except OSError:
            print(f"  ! Watlow error in {stage_name} — retrying in 5s...")
            write_heartbeat()
            time.sleep(5)
            continue

        if heater_on:
            check_ai2(ai2, AI2_SAFETY_LIMIT, stage_name)

        now = time.time()
        if now - last_log_time >= LOG_INTERVAL:
            sp = setpoint if heater_on else 0
            print_row(elapsed_min, sp, ai1, ai2, stage_name,
                      f"({remaining:.1f} min left)")
            log_row(writer, csvfile, elapsed_min, sp, ai1, ai2,
                    flows, stage_name)
            last_log_time = now

        write_heartbeat()
        time.sleep(5)

    return last_log_time


def run_kinetic_steps(tc, mfc, writer, csvfile, stage_prefix,
                      steps, step_minutes, experiment_start, last_log_time,
                      fixed_flows_fn):
    """
    Run a series of kinetic steps with countdown.
    fixed_flows_fn(step_index) → flow dict for logging.
    Returns last_log_time.
    """
    total_steps = len(steps)

    for i, step_flows in enumerate(steps):
        print()
        print(f"  {'='*55}")
        print(f"  {stage_prefix} — Step {i+1} of {total_steps}")
        print(f"  {'='*55}")

        # Set flows for this step
        for ch, flow in step_flows.items():
            mfc.set_channel(ch, flow)

        log_flows = fixed_flows_fn(step_flows)
        print(f"  Flows: { {k: v for k, v in log_flows.items()} }")

        # Hold for step_minutes, with temperature maintained and logging
        # Skip countdown on last step
        if i < total_steps - 1:
            step_start = time.time()
            step_dur   = step_minutes * 60
            while (time.time() - step_start) < step_dur:
                elapsed_min = (time.time() - experiment_start) / 60.0
                remaining   = step_minutes - (time.time() - step_start) / 60.0

                try:
                    tc.set(TARGET_TEMP)
                    ai1, ai2 = tc.read_temperatures()
                except OSError:
                    print(f"  ! Watlow error — retrying in 5s...")
                    write_heartbeat()
                    time.sleep(5)
                    continue

                check_ai2(ai2, AI2_SAFETY_LIMIT, stage_prefix)

                # Console countdown
                mins_r = int(remaining)
                secs_r = int((remaining - mins_r) * 60)
                print(f"  Time remaining: {mins_r:02d}:{secs_r:02d}    ", end='\r')

                now = time.time()
                if now - last_log_time >= LOG_INTERVAL:
                    print()
                    print_row(elapsed_min, TARGET_TEMP, ai1, ai2,
                              stage_prefix, f"step {i+1} ({remaining:.1f} min left)")
                    log_row(writer, csvfile, elapsed_min, TARGET_TEMP,
                            ai1, ai2, log_flows, stage_prefix)
                    last_log_time = now

                write_heartbeat()
                time.sleep(1)

            print()
            print(f"  ✓ Step {i+1} complete")
        else:
            print(f"  Final step flows set — moving to next stage")

    return last_log_time


# ── Main Experiment ───────────────────────────────────────────
def run_experiment():
    tc  = WatlowController(WATLOW_PORT)
    mfc = MFCController(MFC_PORT, MFC_BAUD)

    filename = f"full_kinetics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    print()
    print("=" * 65)
    print("  FULL KINETICS EXPERIMENT")
    print("=" * 65)
    print(f"  Watlow port    : {WATLOW_PORT}")
    print(f"  MFC port       : {MFC_PORT}")
    print(f"  Target temp    : {TARGET_TEMP}°C  at {RAMP_RATE}°C/min")
    print(f"  AI2 limit      : {AI2_SAFETY_LIMIT}°C")
    print(f"  NH3 kinetics   : {NH3_NUM_STEPS} steps x {NH3_STEP_MINUTES} min")
    print(f"  Purge 1        : {PURGE1_DURATION} min  (CH3=24)")
    print(f"  H2  kinetics   : {len(H2_STEPS)} steps x {H2_STEP_MINUTES} min")
    print(f"  Cooldown       : {COOL_DURATION} min  (heater OFF)")
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

            # ── Startup ───────────────────────────────────────
            print("  Checking Watlow connection...")
            try:
                test = tc.get()
                print(f"  Connected. Temp: {test['actual']:.2f}°C  "
                      f"Setpoint: {test['setpoint']:.2f}°C")
            except Exception:
                raise RuntimeError(
                    f"Cannot communicate with Watlow on {WATLOW_PORT}."
                )

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

            # ── STAGE 1: RAMP to 550°C ────────────────────────
            print()
            print("=" * 65)
            print(f"  STAGE 1: RAMP — {start_temp:.1f}°C → {TARGET_TEMP}°C "
                  f"at {RAMP_RATE}°C/min")
            print("=" * 65)
            current_flows = FLOW_RAMP.copy()
            mfc.set_flows(current_flows, "Ramp flows (inert CH3=24)")
            print()

            last_log_time = run_ramp(
                tc, writer, csvfile,
                start_temp, TARGET_TEMP, RAMP_RATE,
                current_flows, experiment_start, last_log_time
            )

            # ── STAGE 2: NH3 KINETICS ─────────────────────────
            print()
            print("=" * 65)
            print(f"  STAGE 2: NH3 KINETICS — {NH3_NUM_STEPS} steps x "
                  f"{NH3_STEP_MINUTES} min at {TARGET_TEMP}°C")
            print("=" * 65)

            # Build NH3 step list
            nh3_steps = []
            for s in range(NH3_NUM_STEPS):
                ch1 = NH3_START_CH1 - s * NH3_STEP_SIZE
                ch4 = NH3_START_CH4 + s * NH3_STEP_SIZE
                nh3_steps.append({1: ch1, 2: 0, 3: 0, 4: ch4})

            def nh3_log_flows(step_flows):
                return step_flows  # already full dict

            last_log_time = run_kinetic_steps(
                tc, mfc, writer, csvfile,
                "NH3_KIN", nh3_steps, NH3_STEP_MINUTES,
                experiment_start, last_log_time, nh3_log_flows
            )

            # ── STAGE 3: PURGE 1 (15 min, CH3=24) ────────────
            print()
            print("=" * 65)
            print(f"  STAGE 3: PURGE 1 — {PURGE1_DURATION} min  (CH3=24)")
            print("=" * 65)
            current_flows = FLOW_PURGE.copy()
            mfc.set_flows(current_flows, "Purge flows (CH3=24)")
            print()

            last_log_time = run_timed_stage(
                tc, writer, csvfile, "PURGE1",
                PURGE1_DURATION, TARGET_TEMP, current_flows,
                experiment_start, last_log_time, heater_on=True
            )

            # ── STAGE 4: H2 KINETICS ──────────────────────────
            print()
            print("=" * 65)
            print(f"  STAGE 4: H2 KINETICS — {len(H2_STEPS)} steps x "
                  f"{H2_STEP_MINUTES} min at {TARGET_TEMP}°C")
            print("=" * 65)

            # Build H2 step list (CH1=20, CH2=0 always)
            h2_steps = []
            for ch3_flow, ch4_flow in H2_STEPS:
                h2_steps.append({1: 20, 2: 0, 3: ch3_flow, 4: ch4_flow})

            def h2_log_flows(step_flows):
                return step_flows

            last_log_time = run_kinetic_steps(
                tc, mfc, writer, csvfile,
                "H2_KIN", h2_steps, H2_STEP_MINUTES,
                experiment_start, last_log_time, h2_log_flows
            )

            # ── STAGE 5: HEATER OFF + 30 MIN COOL ────────────
            print()
            print("=" * 65)
            print(f"  STAGE 5: COOL — heater OFF, {COOL_DURATION} min purge")
            print("=" * 65)
            tc.set(25.0)
            tc.set_control_loop('off')
            current_flows = FLOW_COOL.copy()
            mfc.set_flows(current_flows, "Cooldown flows (CH1=20, CH4=4)")
            print()

            last_log_time = run_timed_stage(
                tc, writer, csvfile, "COOL",
                COOL_DURATION, 0, current_flows,
                experiment_start, last_log_time, heater_on=False
            )

            # ── STAGE 6: FINAL FLOW ───────────────────────────
            print()
            print("=" * 65)
            print("  STAGE 6: END — final flow (CH3=24)")
            print("=" * 65)
            current_flows = FLOW_END.copy()
            mfc.set_flows(current_flows, "Final flows (CH3=24)")
            print()
            print("  Experiment complete! Press Ctrl+C to stop logging.")
            print()

            while True:
                elapsed_min = (time.time() - experiment_start) / 60.0
                try:
                    ai1, ai2 = tc.read_temperatures()
                except OSError:
                    ai1, ai2 = None, None

                now = time.time()
                if now - last_log_time >= LOG_INTERVAL:
                    print_row(elapsed_min, 0, ai1, ai2, "END")
                    log_row(writer, csvfile, elapsed_min, 0,
                            ai1, ai2, current_flows, "END")
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
            mfc.set_flows(FLOW_SAFE, "Safe flows (CH3=24)")
        except Exception as e:
            print(f"  ! MFC error: {e}")
        try:
            tc.set(25.0)
            tc.set_control_loop('off')
        except Exception as e:
            print(f"  ! Watlow error: {e}")
        try:
            tc.close()
            mfc.close()
        except Exception:
            pass
        print(f"  Data saved to: {filename}")
        print("  Done.")


if __name__ == '__main__':
    run_experiment()
