"""
Real-Time AI2 Temperature Plotter
------------------------------------
Continuously reads AI2 (surface thermocouple) from the Watlow
and plots it live, updating every second.

Features:
  - Live scrolling plot (shows last N minutes)
  - Also plots AI1 (bed temp) for reference
  - Current temperature display
  - Min / Max / Average stats
  - AI2 safety limit line
  - CSV logging while plotting

Usage:
    python ai2_plotter.py
"""

import struct
import time
import csv
from datetime import datetime
from collections import deque
from binascii import unhexlify

import serial
import crcmod
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from matplotlib.gridspec import GridSpec

# ═══════════════════════════════════════════════════════════════
#  SETTINGS
# ═══════════════════════════════════════════════════════════════

WATLOW_PORT      = 'COM4'
AI2_SAFETY_LIMIT = 85.0      # °C — shown as a red dashed line
WINDOW_MINUTES   = 30        # how many minutes to show on the x-axis
UPDATE_INTERVAL  = 1000      # milliseconds between plot updates (1 second)
LOG_TO_CSV       = True      # save readings to a CSV file

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

    def read_ai1(self):
        resp = self._send('0510000006', '010301040101')
        return f_to_c(struct.unpack('>f', unhexlify(resp[30:38]))[0])

    def read_ai2(self):
        resp = self._send('0510000006', '010301040102')
        return f_to_c(struct.unpack('>f', unhexlify(resp[30:38]))[0])

    def read_both(self):
        try:
            ai1 = self.read_ai1()
            ai2 = self.read_ai2()
            return ai1, ai2
        except Exception:
            return None, None

    def close(self):
        self.connection.close()


# ── Plotter ───────────────────────────────────────────────────
class LivePlotter:

    def __init__(self, tc, csvfile, writer):
        self.tc      = tc
        self.csvfile = csvfile
        self.writer  = writer

        # Data buffers — keep WINDOW_MINUTES * 60 points (1 per second)
        maxlen = WINDOW_MINUTES * 60
        self.times = deque(maxlen=maxlen)   # elapsed seconds
        self.ai1   = deque(maxlen=maxlen)
        self.ai2   = deque(maxlen=maxlen)
        self.start = time.time()

        self._setup_plot()

    def _setup_plot(self):
        plt.style.use('dark_background')
        self.fig = plt.figure(figsize=(13, 7), facecolor='#0d0d0d')
        self.fig.canvas.manager.set_window_title('AI2 Live Temperature Monitor')

        gs = GridSpec(2, 3, figure=self.fig,
                      hspace=0.45, wspace=0.35,
                      left=0.07, right=0.97,
                      top=0.88, bottom=0.12)

        # Main plot — spans top 2 columns
        self.ax_main = self.fig.add_subplot(gs[0, :2])
        # AI2 only plot — top right
        self.ax_ai2  = self.fig.add_subplot(gs[0, 2])
        # Stats boxes — bottom row
        self.ax_s1   = self.fig.add_subplot(gs[1, 0])
        self.ax_s2   = self.fig.add_subplot(gs[1, 1])
        self.ax_s3   = self.fig.add_subplot(gs[1, 2])

        # ── Main plot ──────────────────────────────────────
        self.ax_main.set_facecolor('#111111')
        self.ax_main.set_title('AI1 Bed & AI2 Surface Temperature',
                               color='#cccccc', fontsize=11, pad=8)
        self.ax_main.set_xlabel('Elapsed Time (min)', color='#888888', fontsize=9)
        self.ax_main.set_ylabel('Temperature (°C)',   color='#888888', fontsize=9)
        self.ax_main.tick_params(colors='#666666', labelsize=8)
        for spine in self.ax_main.spines.values():
            spine.set_color('#333333')
        self.ax_main.grid(True, color='#222222', linewidth=0.5, linestyle='--')

        self.line_ai1, = self.ax_main.plot([], [], color='#4fc3f7',
                                           linewidth=1.2, label='AI1 Bed',
                                           alpha=0.85)
        self.line_ai2_main, = self.ax_main.plot([], [], color='#ff7043',
                                                linewidth=1.8, label='AI2 Surface')
        self.ax_main.axhline(y=AI2_SAFETY_LIMIT, color='#ef5350',
                             linewidth=1.0, linestyle='--', alpha=0.6,
                             label=f'AI2 Limit ({AI2_SAFETY_LIMIT}°C)')
        self.ax_main.legend(loc='upper left', fontsize=8,
                            facecolor='#1a1a1a', edgecolor='#333333',
                            labelcolor='#cccccc')

        # ── AI2 zoom plot ──────────────────────────────────
        self.ax_ai2.set_facecolor('#111111')
        self.ax_ai2.set_title('AI2 Surface (last 5 min)',
                              color='#cccccc', fontsize=10, pad=8)
        self.ax_ai2.set_xlabel('Elapsed Time (min)', color='#888888', fontsize=8)
        self.ax_ai2.set_ylabel('°C', color='#888888', fontsize=8)
        self.ax_ai2.tick_params(colors='#666666', labelsize=8)
        for spine in self.ax_ai2.spines.values():
            spine.set_color('#333333')
        self.ax_ai2.grid(True, color='#222222', linewidth=0.5, linestyle='--')

        self.line_ai2_zoom, = self.ax_ai2.plot([], [], color='#ff7043',
                                               linewidth=1.8)
        self.ax_ai2.axhline(y=AI2_SAFETY_LIMIT, color='#ef5350',
                            linewidth=1.0, linestyle='--', alpha=0.6)

        # ── Stats boxes (turn off axes) ────────────────────
        for ax in [self.ax_s1, self.ax_s2, self.ax_s3]:
            ax.set_facecolor('#111111')
            ax.axis('off')
            for spine in ax.spines.values():
                spine.set_color('#333333')

        # Current temp display
        self.txt_ai2_val = self.ax_s1.text(
            0.5, 0.55, '--.---°C', transform=self.ax_s1.transAxes,
            ha='center', va='center', fontsize=22, fontweight='bold',
            color='#ff7043', fontfamily='monospace'
        )
        self.ax_s1.text(0.5, 0.15, 'AI2 Surface Now',
                        transform=self.ax_s1.transAxes,
                        ha='center', va='center', fontsize=9, color='#888888')

        self.txt_ai1_val = self.ax_s2.text(
            0.5, 0.55, '--.---°C', transform=self.ax_s2.transAxes,
            ha='center', va='center', fontsize=22, fontweight='bold',
            color='#4fc3f7', fontfamily='monospace'
        )
        self.ax_s2.text(0.5, 0.15, 'AI1 Bed Now',
                        transform=self.ax_s2.transAxes,
                        ha='center', va='center', fontsize=9, color='#888888')

        # Min / Max / Avg stats for AI2
        self.txt_stats = self.ax_s3.text(
            0.5, 0.55, 'Min: --\nMax: --\nAvg: --\nΔ:   --',
            transform=self.ax_s3.transAxes,
            ha='center', va='center', fontsize=11,
            color='#cccccc', fontfamily='monospace',
            linespacing=1.8
        )
        self.ax_s3.text(0.5, 0.05, 'AI2 Session Stats  (min/max/avg/spread)',
                        transform=self.ax_s3.transAxes,
                        ha='center', va='center', fontsize=9, color='#888888')

        # Title bar with timestamp
        self.fig.suptitle('Watlow Real-Time Monitor',
                          color='#eeeeee', fontsize=13, fontweight='bold', y=0.97)
        self.txt_time = self.fig.text(0.97, 0.97, '',
                                      ha='right', va='top',
                                      color='#555555', fontsize=8,
                                      fontfamily='monospace')

        plt.tight_layout(rect=[0, 0, 1, 0.95])

    def update(self, frame):
        ai1, ai2 = self.tc.read_both()
        now = time.time()
        elapsed_min = (now - self.start) / 60.0

        if ai1 is not None and ai2 is not None:
            self.times.append(elapsed_min)
            self.ai1.append(ai1)
            self.ai2.append(ai2)

            # CSV logging
            if self.writer:
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.writer.writerow([ts, f"{elapsed_min:.4f}",
                                      f"{ai1:.4f}", f"{ai2:.4f}"])
                self.csvfile.flush()

        t  = list(self.times)
        a1 = list(self.ai1)
        a2 = list(self.ai2)

        if not t:
            return

        # ── Update main plot ───────────────────────────────
        self.line_ai1.set_data(t, a1)
        self.line_ai2_main.set_data(t, a2)
        self.ax_main.set_xlim(max(0, t[-1] - WINDOW_MINUTES), t[-1] + 0.5)
        # Tight autoscale around AI2 only (ignore AI1 for y range)
        if a2:
            margin = max((max(a2) - min(a2)) * 0.3, 0.1)
            self.ax_main.set_ylim(min(a2) - margin, max(a2) + margin)

        # ── Update AI2 zoom (last 5 min) ───────────────────
        cutoff = t[-1] - 5.0
        t5  = [x for x in t  if x >= cutoff]
        a2_5 = a2[len(a2) - len(t5):]
        self.line_ai2_zoom.set_data(t5, a2_5)
        self.ax_ai2.set_xlim(cutoff, t[-1] + 0.1)
        if a2_5:
            margin = max((max(a2_5) - min(a2_5)) * 0.3, 0.05)
            self.ax_ai2.set_ylim(min(a2_5) - margin, max(a2_5) + margin)

        # ── Update stat displays ───────────────────────────
        if ai2 is not None:
            color = '#ef5350' if ai2 >= AI2_SAFETY_LIMIT else '#ff7043'
            self.txt_ai2_val.set_text(f'{ai2:.3f}°C')
            self.txt_ai2_val.set_color(color)

        if ai1 is not None:
            self.txt_ai1_val.set_text(f'{ai1:.3f}°C')

        if a2:
            mn  = min(a2)
            mx  = max(a2)
            avg = sum(a2) / len(a2)
            spread = mx - mn
            self.txt_stats.set_text(
                f'Min:  {mn:.3f}°C\nMax:  {mx:.3f}°C\nAvg:  {avg:.3f}°C\nΔ:    {spread:.3f}°C'
            )

        # ── Timestamp ──────────────────────────────────────
        self.txt_time.set_text(datetime.now().strftime('%Y-%m-%d  %H:%M:%S'))

        return (self.line_ai1, self.line_ai2_main, self.line_ai2_zoom,
                self.txt_ai2_val, self.txt_ai1_val, self.txt_stats,
                self.txt_time)

    def run(self):
        self.ani = animation.FuncAnimation(
            self.fig, self.update,
            interval=UPDATE_INTERVAL,
            blit=False,
            cache_frame_data=False
        )
        plt.show()


# ── Main ──────────────────────────────────────────────────────
def main():
    print()
    print("=" * 55)
    print("  AI2 LIVE TEMPERATURE PLOTTER")
    print("=" * 55)
    print(f"  Watlow port    : {WATLOW_PORT}")
    print(f"  Update rate    : every {UPDATE_INTERVAL//1000}s")
    print(f"  Window         : last {WINDOW_MINUTES} min")
    print(f"  AI2 limit line : {AI2_SAFETY_LIMIT}°C")
    print(f"  CSV logging    : {'ON' if LOG_TO_CSV else 'OFF'}")
    print("=" * 55)
    print("  Close the plot window to stop.")
    print("=" * 55)
    print()

    print("  Connecting to Watlow...")
    try:
        tc = WatlowController(WATLOW_PORT)
        ai1, ai2 = tc.read_both()
        if ai1 is None:
            raise RuntimeError("No response from Watlow.")
        print(f"  Connected. AI1={ai1:.1f}°C  AI2={ai2:.1f}°C")
    except Exception as e:
        print(f"  ERROR: {e}")
        print(f"  Check that {WATLOW_PORT} is correct and not in use.")
        return

    csvfile = None
    writer  = None
    filename = None

    if LOG_TO_CSV:
        filename = f"ai2_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        csvfile  = open(filename, 'w', newline='')
        import csv as _csv
        writer = _csv.writer(csvfile)
        writer.writerow(['timestamp', 'elapsed_min', 'AI1_bed_C', 'AI2_surface_C'])
        print(f"  Logging to    : {filename}")

    print()
    print("  Opening plot window...")

    try:
        plotter = LivePlotter(tc, csvfile, writer)
        plotter.run()
    finally:
        tc.close()
        if csvfile:
            csvfile.close()
            print(f"\n  Data saved to: {filename}")
        print("  Done.")


if __name__ == '__main__':
    import csv
    main()
