"""
MFC Channel Reader — Brooks Instrument 0250 Series
----------------------------------------------------
Reads the present value (actual flow) and setpoint for all 4 channels.

Protocol from Brooks 0250 Series manual (Appendix C):
  - Actual flow  : AZ.xxK     (input port:  CH1=01, CH2=03, CH3=05, CH4=07)
  - Setpoint read: AZ.xxP01?  (output port: CH1=02, CH2=04, CH3=06, CH4=08)

Usage:
    python mfc_reader.py
"""

import serial
import time

# ═══════════════════════════════════════════════════════════════
MFC_PORT = 'COM2'
MFC_BAUD = 9600
# ═══════════════════════════════════════════════════════════════

# Channel → (input_port, output_port)  per Brooks 0250 manual
CHANNEL_PORTS = {
    1: ('01', '02'),
    2: ('03', '04'),
    3: ('05', '06'),
    4: ('07', '08'),
}


def send(ser, command):
    ser.reset_input_buffer()
    ser.write((command + '\r').encode())
    time.sleep(0.2)
    return ser.readline().decode(errors='replace').strip()


def parse_k_response(response):
    """
    K response: AZ,address.port,2,non_reset_total,total,process_value,...,checksum
    Field index 6 = actual process value (flow).
    """
    try:
        fields = response.split(',')
        if len(fields) >= 7 and fields[0] == 'AZ':
            return float(fields[6])
    except (ValueError, IndexError):
        pass
    return None


def parse_p_response(response):
    """
    P01? response: AZ,address.port,4,P01,value,checksum
    Field index 4 = the setpoint value.
    """
    try:
        fields = response.split(',')
        if len(fields) >= 6 and fields[0] == 'AZ':
            return float(fields[4])
    except (ValueError, IndexError):
        pass
    return None


def read_channel(ser, channel):
    input_port, output_port = CHANNEL_PORTS[channel]
    k_raw = send(ser, f'AZ.{input_port}K')
    p_raw = send(ser, f'AZ.{output_port}P01?')
    return parse_k_response(k_raw), parse_p_response(p_raw), k_raw, p_raw


def main():
    print()
    print("=" * 65)
    print("  MFC CHANNEL READER — Brooks Instrument 0250 Series")
    print("=" * 65)
    print(f"  Port: {MFC_PORT}  Baud: {MFC_BAUD}")
    print("=" * 65)
    print()

    try:
        ser = serial.Serial(
            port=MFC_PORT, baudrate=MFC_BAUD,
            bytesize=8, parity='N',
            stopbits=1, timeout=2
        )
        time.sleep(0.3)
    except Exception as e:
        print(f"  ERROR: Could not open {MFC_PORT}: {e}")
        return

    # Sync command — clears any stale state in the controller
    ser.write(b'\x1bAZ\r')
    time.sleep(0.3)
    ser.reset_input_buffer()

    print(f"  {'Channel':<10} {'Actual Flow':>16} {'Setpoint':>14}")
    print(f"  {'-'*10} {'-'*16} {'-'*14}")

    for ch in [1, 2, 3, 4]:
        actual, setpoint, k_raw, p_raw = read_channel(ser, ch)

        actual_str   = f"{actual:.3f} mL/min"   if actual   is not None else "ERROR"
        setpoint_str = f"{setpoint:.3f} mL/min" if setpoint is not None else "ERROR"
        print(f"  CH{ch:<8} {actual_str:>16} {setpoint_str:>14}")

        if actual is None or setpoint is None:
            print(f"    K   raw: {repr(k_raw)}")
            print(f"    P01 raw: {repr(p_raw)}")

    print()
    ser.close()


if __name__ == '__main__':
    main()
