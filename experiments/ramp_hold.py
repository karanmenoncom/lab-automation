"""
Simple Ramp & Hold Temperature Controller
-------------------------------------------
Stages:
  RAMP : Current → Target at ramp rate
  HOLD : Hold at target temperature indefinitely (Ctrl+C to stop)

No MFC control — temperature only.

Usage:
    python ramp_hold.py
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
TARGET_TEMP      = 400.0    # °C — ramp target
RAMP_RATE        = 10.0     # °C/min
AI2_SAFETY_LIMIT = 85.0     # °C — AI2 shutoff threshold
HOLD_DURATION    = 3 * 60   # minutes — hold at target temp
LOG_INTERVAL     = 5 * 60   # seconds (5 minutes)

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


# ── Helpers ───────────────────────────────────────────────────
def log_row(writer, csvfile, elapsed_min, setpoint, ai1, ai2, stage):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    writer.writerow([
        ts, f"{elapsed_min:.2f}", f"{setpoint:.2f}",
        f"{ai1:.2f}" if ai1 is not None else "ERR",
        f"{ai2:.2f}" if ai2 is not None else "ERR",
        stage
    ])
    csvfile.flush()


def print_row(elapsed_min, setpoint, ai1, ai2, stage, extra=''):
    sp = f"{setpoint:.2f}°" if setpoint > 0 else "OFF"
    a1 = f"{ai1:.2f}°" if ai1 is not None else "ERR"
    a2 = f"{ai2:.2f}°" if ai2 is not None else "ERR"
    print(f"  {elapsed_min:>10.2f} {sp:>10} {a1:>10} {a2:>10}  [{stage}]  {extra}")


# ── Main Experiment ───────────────────────────────────────────
def run_experiment():
    tc = WatlowController(WATLOW_PORT)

    filename = f"ramp_hold_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    print()
    print("=" * 65)
    print("  RAMP & HOLD")
    print("=" * 65)
    print(f"  Watlow port    : {WATLOW_PORT}")
    print(f"  Target temp    : {TARGET_TEMP}°C")
    print(f"  Ramp rate      : {RAMP_RATE}°C/min")
    print(f"  AI2 limit      : {AI2_SAFETY_LIMIT}°C")
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
                'AI1_bed_C', 'AI2_surface_C', 'stage'
            ])

            # ── Startup ───────────────────────────────────────
            print("  Checking Watlow connection...")
            try:
                test = tc.get()
                print(f"  Connected. Current temp: {test['actual']:.2f}°C  "
                      f"Setpoint: {test['setpoint']:.2f}°C")
            except Exception:
                raise RuntimeError(
                    f"Cannot communicate with Watlow on {WATLOW_PORT}."
                )

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
            print()

            ramp_start = time.time()
            while True:
                elapsed_min  = (time.time() - experiment_start) / 60.0
                ramp_elapsed = (time.time() - ramp_start) / 60.0
                setpoint     = min(start_temp + RAMP_RATE * ramp_elapsed, TARGET_TEMP)

                try:
                    tc.set(setpoint)
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
                    print_row(elapsed_min, setpoint, ai1, ai2, "RAMP")
                    log_row(writer, csvfile, elapsed_min, setpoint,
                            ai1, ai2, "RAMP")
                    last_log_time = now

                if setpoint >= TARGET_TEMP:
                    print(f"\n  ✓ Reached {TARGET_TEMP}°C")
                    break

                write_heartbeat()
                time.sleep(5)

            # ── STAGE 2: HOLD ─────────────────────────────────
            print()
            print("=" * 65)
            print(f"  STAGE 2: HOLD — {TARGET_TEMP}°C for {HOLD_DURATION} min")
            print("=" * 65)
            print()

            hold_start = time.time()
            while (time.time() - hold_start) < HOLD_DURATION * 60:
                elapsed_min = (time.time() - experiment_start) / 60.0
                remaining   = HOLD_DURATION - (time.time() - hold_start) / 60.0

                try:
                    tc.set(TARGET_TEMP)
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
                    print_row(elapsed_min, TARGET_TEMP, ai1, ai2, "HOLD",
                              f"({remaining:.1f} min left)")
                    log_row(writer, csvfile, elapsed_min, TARGET_TEMP,
                            ai1, ai2, "HOLD")
                    last_log_time = now

                write_heartbeat()
                time.sleep(5)

            print(f"\n  Hold complete ({HOLD_DURATION} min) — shutting down")

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
            tc.set(25.0)
            tc.set_control_loop('off')
        except Exception as e:
            print(f"  ! Watlow error during shutdown: {e}")
        try:
            tc.close()
        except Exception:
            pass
        print(f"  Data saved to: {filename}")
        print("  Done.")


if __name__ == '__main__':
    run_experiment()
