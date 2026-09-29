"""
Temperature Ramp Controller for Watlow EZ-Zone
-----------------------------------------------
- Turns control loop ON automatically
- Ramps bed temperature (AI1) from current temp to 100°C at 10°C/min
- Logs AI1 (bed) and AI2 (surface) every 5 seconds to CSV
- Holds at 100°C once reached
- Turns control loop OFF safely on Ctrl+C or when done

Usage:
    python temperature_ramp.py
"""

import struct
import csv
import time
from datetime import datetime
from binascii import unhexlify

import watlow
from watlow.driver import crc8, crc16, f_to_c

# ── Settings ─────────────────────────────────────────────────────────────────
PORT         = 'COM4'
TARGET_TEMP  = 100.0   # °C
RAMP_RATE    = 10.0    # °C/min
UPDATE_EVERY = 5       # seconds between each log + setpoint update
# ─────────────────────────────────────────────────────────────────────────────


class WatlowController(watlow.TemperatureController):
    """Watlow driver with AI2 support and control loop on/off."""

    def __init__(self, port, timeout=0.5):
        super().__init__(port, timeout)
        # AI2 surface thermocouple register (found via debugging)
        self.commands['analog_input_2'] = {
            'header': unhexlify('0510000006'),
            'body':   unhexlify('010301040102'),
        }

    def _send(self, header_hex, body_hex):
        """Send a raw command and return the response."""
        preamble = unhexlify('55ff')
        header   = unhexlify(header_hex)
        body     = unhexlify(body_hex)
        hcrc = struct.pack('<H', ~crc8(header) & 0xff)
        dcrc = struct.pack('<H', ~crc16(body) & 0xffff)
        self.connection.flush()
        self.connection.write(preamble + header + hcrc[:1] + body + dcrc)
        return bytes.hex(self.connection.read(22))

    def set_control_loop(self, mode):
        """Turn the control loop on or off. mode: 'on' or 'off'"""
        value = '000a' if mode == 'on' else '003e'
        self._send('0510030009', f'01040801010f01{value}')

    def get_analog_input_2(self):
        """Read surface thermocouple (AI2), returns degrees C."""
        preamble = unhexlify('55ff')
        key = 'analog_input_2'
        header = self.commands[key]['header']
        body   = self.commands[key]['body']
        hcrc = struct.pack('<H', ~crc8(header) & 0xff)
        dcrc = struct.pack('<H', ~crc16(body) & 0xffff)
        self.connection.flush()
        self.connection.write(preamble + header + hcrc[:1] + body + dcrc)
        response = self.connection.read(22)
        float_hex = bytes.hex(response)[30:38]
        return f_to_c(struct.unpack('>f', unhexlify(float_hex))[0])


def run_ramp():
    tc = WatlowController(PORT)
    filename = f"ramp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    print("=" * 58)
    print("  Watlow Temperature Ramp Controller")
    print("=" * 58)
    print(f"  Port        : {PORT}")
    print(f"  Target      : {TARGET_TEMP} °C")
    print(f"  Ramp rate   : {RAMP_RATE} °C/min")
    print(f"  Log interval: every {UPDATE_EVERY}s")
    print(f"  CSV file    : {filename}")
    print("=" * 58)
    print("  Press Ctrl+C to stop safely at any time")
    print("=" * 58)
    print()

    try:
        with open(filename, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow([
                'timestamp', 'elapsed_min',
                'setpoint_C', 'AI1_bed_C', 'AI2_surface_C', 'status'
            ])

            # Turn control loop ON
            print("  Turning control loop ON...")
            tc.set_control_loop('on')
            time.sleep(1)

            # Read starting temperature
            start_temp = tc.get()['actual']
            start_time = time.time()
            print(f"  Starting temperature: {start_temp:.2f} °C")
            print(f"  Ramping to {TARGET_TEMP} °C at {RAMP_RATE} °C/min...")
            print()
            print(f"  {'Time(min)':>10} {'Setpoint':>10} {'AI1 Bed':>10} {'AI2 Surface':>12} {'Status':>6}")
            print(f"  {'-'*10:>10} {'-'*10:>10} {'-'*10:>10} {'-'*12:>12} {'-'*6:>6}")

            holding = False

            while True:
                elapsed_min = (time.time() - start_time) / 60.0

                # Calculate ramp setpoint
                ramp_setpoint = start_temp + (RAMP_RATE * elapsed_min)

                # Cap at target
                if ramp_setpoint >= TARGET_TEMP:
                    ramp_setpoint = TARGET_TEMP
                    if not holding:
                        holding = True
                        print()
                        print(f"  Reached {TARGET_TEMP} °C - now holding.")
                        print()

                status = "HOLD" if holding else "RAMP"

                # Send setpoint and read temperatures
                try:
                    tc.set(ramp_setpoint)
                    data = tc.get()
                    ai1  = data['actual']
                    ai2  = tc.get_analog_input_2()
                except IOError:
                    print("  ! Disconnected, retrying...")
                    time.sleep(1)
                    continue

                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                # Safety check: shut off if AI2 exceeds 30°C
                if ai2 > 30.0:
                    print()
                    print(f"  !! AI2 surface temperature exceeded 30°C ({ai2:.2f}°C) — shutting off!")
                    writer.writerow([ts, f"{elapsed_min:.3f}", f"{ramp_setpoint:.2f}",
                                      f"{ai1:.2f}", f"{ai2:.2f}", "SHUTOFF"])
                    csvfile.flush()
                    raise KeyboardInterrupt

                # Print to screen
                print(f"  {elapsed_min:>10.2f} {ramp_setpoint:>9.2f}° "
                      f"{ai1:>9.2f}° {ai2:>11.2f}°  [{status}]")

                # Save to CSV immediately
                writer.writerow([ts, f"{elapsed_min:.3f}", f"{ramp_setpoint:.2f}",
                                  f"{ai1:.2f}", f"{ai2:.2f}", status])
                csvfile.flush()

                time.sleep(UPDATE_EVERY)

    except KeyboardInterrupt:
        print()
        print("  Stopped by user.")

    finally:
        # Safely freeze setpoint and turn off control loop
        try:
            current = tc.get()['actual']
            tc.set(current)
            tc.set_control_loop('off')
            print(f"  Control loop OFF. Setpoint frozen at {current:.2f} °C.")
        except Exception:
            pass
        tc.close()
        print(f"  Data saved to: {filename}")
        print("  Done.")


if __name__ == '__main__':
    run_ramp()
