"""
Watlow Watchdog
----------------
Runs as a SEPARATE, INDEPENDENT script alongside any experiment.

How it works:
  - The experiment scripts write a small "heartbeat" file every few seconds
  - This watchdog reads that file every CHECK_INTERVAL seconds
  - If the heartbeat is older than TIMEOUT seconds → assumes crash/power failure
  - Immediately sets Watlow setpoint to 25°C and turns control loop OFF
  - Keeps retrying the shutdown until it succeeds

Run this in a SEPARATE terminal window BEFORE starting your experiment:
    python watchdog.py

Then start your experiment in another terminal:
    python full_kinetics.py   (or any other script)

The watchdog will keep running and monitoring until you close it.
If the experiment finishes normally, the watchdog just keeps watching
harmlessly — it only acts if the heartbeat goes stale.

IMPORTANT: This script must be running BEFORE the experiment starts.
"""

import struct
import time
import os
from datetime import datetime
from binascii import unhexlify

import serial
import crcmod

# ═══════════════════════════════════════════════════════════════
#  SETTINGS
# ═══════════════════════════════════════════════════════════════

WATLOW_PORT      = 'COM4'
HEARTBEAT_FILE   = 'experiment_heartbeat.txt'   # shared with experiment scripts
TIMEOUT          = 30       # seconds — if heartbeat older than this → shutdown
CHECK_INTERVAL   = 10       # seconds — how often watchdog checks the heartbeat
RETRY_INTERVAL   = 5        # seconds — how often to retry if shutdown fails

# ═══════════════════════════════════════════════════════════════


# ── CRC functions ─────────────────────────────────────────────
crc8  = crcmod.mkCrcFun(0b110000001)
crc16 = crcmod.mkCrcFun(0b10001000000100001)

def f_to_c(f):
    return (f - 32.0) / 1.8


# ── Minimal Watlow shutdown driver ───────────────────────────
# Kept completely independent — no shared state with experiment
class WatlowShutdown:

    def __init__(self, port):
        self.port = port
        self.connection = None

    def connect(self):
        self.connection = serial.Serial(self.port, 38400, timeout=2.0)
        self.connection.reset_input_buffer()
        self.connection.reset_output_buffer()
        time.sleep(0.3)

    def _send(self, header_hex, body_hex, read_bytes=21):
        preamble = unhexlify('55ff')
        header   = unhexlify(header_hex)
        body     = unhexlify(body_hex)
        hcrc = struct.pack('<H', ~crc8(header) & 0xff)
        dcrc = struct.pack('<H', ~crc16(body) & 0xffff)
        self.connection.reset_input_buffer()
        self.connection.write(preamble + header + hcrc[:1] + body + dcrc)
        return bytes.hex(self.connection.read(read_bytes))

    def set_setpoint(self, setpoint_c):
        setpoint_f = setpoint_c * 1.8 + 32.0
        body = unhexlify('010407010108') + struct.pack('>f', setpoint_f)
        header = unhexlify('051000000a')
        hcrc = struct.pack('<H', ~crc8(header) & 0xff)
        dcrc = struct.pack('<H', ~crc16(body) & 0xffff)
        self.connection.reset_input_buffer()
        self.connection.write(unhexlify('55ff') + header + hcrc[:1] + body + dcrc)
        self.connection.read(20)

    def set_control_loop_off(self):
        self._send('0510030009', '01040801010f01003e')

    def emergency_shutdown(self):
        """Set setpoint to 25°C and turn control loop off. Returns True on success."""
        try:
            self.connect()
            self.set_setpoint(25.0)
            time.sleep(0.3)
            self.set_control_loop_off()
            time.sleep(0.3)
            self.connection.close()
            return True
        except Exception as e:
            print(f"  [Watchdog] Shutdown attempt failed: {e}")
            try:
                self.connection.close()
            except Exception:
                pass
            return False


# ── Heartbeat helpers ─────────────────────────────────────────
def heartbeat_age():
    """Returns age of heartbeat file in seconds, or None if missing."""
    if not os.path.exists(HEARTBEAT_FILE):
        return None
    try:
        with open(HEARTBEAT_FILE, 'r') as f:
            ts = float(f.read().strip())
        return time.time() - ts
    except Exception:
        return None


def stamp(msg):
    return f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"


# ── Main watchdog loop ────────────────────────────────────────
def main():
    print()
    print("=" * 60)
    print("  WATLOW WATCHDOG")
    print("=" * 60)
    print(f"  Watlow port      : {WATLOW_PORT}")
    print(f"  Heartbeat file   : {HEARTBEAT_FILE}")
    print(f"  Timeout          : {TIMEOUT}s  (crash assumed after this)")
    print(f"  Check interval   : every {CHECK_INTERVAL}s")
    print("=" * 60)
    print("  Waiting for experiment to start...")
    print("  (Start your experiment script in another terminal)")
    print("=" * 60)
    print()

    tc = WatlowShutdown(WATLOW_PORT)
    shutdown_done = False
    experiment_seen = False

    try:
        while True:
            age = heartbeat_age()

            if age is None:
                # Heartbeat file doesn't exist yet
                if experiment_seen:
                    # It existed before and now it's gone — treat as crash
                    print(stamp(" Heartbeat file missing! Treating as crash."))
                    age = TIMEOUT + 1   # force shutdown below
                else:
                    print(stamp(f" Waiting for heartbeat file ({HEARTBEAT_FILE})..."),
                          end='\r')
                    time.sleep(CHECK_INTERVAL)
                    continue

            if not experiment_seen and age < TIMEOUT:
                experiment_seen = True
                print()
                print(stamp(" Heartbeat detected — experiment is running."))
                print(stamp(f" Monitoring every {CHECK_INTERVAL}s. "
                            f"Will act if silent for >{TIMEOUT}s."))
                print()
                shutdown_done = False

            if age < TIMEOUT:
                # All good
                print(stamp(f" OK — heartbeat age: {age:.1f}s"), end='\r')
                shutdown_done = False
                time.sleep(CHECK_INTERVAL)

            else:
                # Heartbeat is stale — experiment crashed or computer restarted
                if not shutdown_done:
                    print()
                    print("!" * 60)
                    print(stamp(f" HEARTBEAT STALE ({age:.0f}s > {TIMEOUT}s limit)"))
                    print(stamp(" EXPERIMENT APPEARS TO HAVE CRASHED"))
                    print(stamp(" INITIATING EMERGENCY SHUTDOWN..."))
                    print("!" * 60)

                    attempt = 1
                    while not shutdown_done:
                        print(stamp(f" Shutdown attempt {attempt}..."))
                        success = tc.emergency_shutdown()
                        if success:
                            print(stamp(" ✓ Watlow set to 25°C, control loop OFF"))
                            print(stamp(" Watlow is safe."))
                            shutdown_done = True
                        else:
                            print(stamp(f" Retrying in {RETRY_INTERVAL}s..."))
                            attempt += 1
                            time.sleep(RETRY_INTERVAL)
                else:
                    # Already shut down — just keep reporting
                    print(stamp(f" Watlow already shut down. "
                                f"Heartbeat still stale ({age:.0f}s)."), end='\r')
                    time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print()
        print()
        print("  Watchdog stopped by user.")
        print("  NOTE: Watlow is NOT automatically shut down when watchdog stops.")
        print("  Make sure your experiment is still running or shut down manually.")
        print()


if __name__ == '__main__':
    main()
