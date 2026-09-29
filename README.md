# Lab Automation Catalysis Reactor Control

**Disclaimer:** This project is not affiliated with or endorsed by Watlow Electric Manufacturing Company, Harrick Scientific Products, Inc., Specac Ltd or Brooks Instrument. All product names are trademarks of their respective owners.

Python scripts for automated control of a catalysis reactor rig using:
- **Watlow EZ-Zone** temperature controller (RS-422/485 serial, BACnet MS/TP protocol)
- **Brooks Instrument 0250 Series** Mass Flow Controller (RS-232/485, proprietary ASCII protocol)

---

## Hardware Setup

| Device | Port | Baud | Protocol |
|--------|------|------|----------|
| Watlow EZ-Zone | COM4 | 38400 | BACnet MS/TP (RS-422) |
| Brooks 0250 MFC | COM2 | 9600 | ASCII serial (RS-232) |

You will need a **USB → RS-422** adapter for the Watlow.  
The Watlow uses two thermocouples:
- **AI1** — bed thermocouple (register `040101`)
- **AI2** — surface thermocouple (register `040401`)

### MFC Channel Mapping

| Channel | Gas | Input Port | Output Port |
|---------|-----|-----------|-------------|
| CH1 | NH₃  | 01 | 02 |
| CH2 | N₂ | 03 | 04 |
| CH3 | He | 05 | 06 |
| CH4 | N₂ / He| 07 | 08 |

---

## Protocol Notes

### Watlow EZ-Zone (BACnet MS/TP)

Frame format:
```
55ff  [preamble]
05    [request marker]
10    [zone, always 10]
XXXXXX [header bytes + length]
CC    [CRC8 of header, polynomial 0b110000001]
XXXXXX [register + instance bytes]
XXXXXXXX [32-bit big-endian IEEE float value, in Fahrenheit]
CCCC  [CRC16 of body, polynomial 0b10001000000100001, BACnet spec]
```

Registers used:
- `040101` — AI1 actual temperature (read)
- `040401` — AI2 actual temperature (read)
- `070101` — setpoint (read/write)
- `080101` — control loop ON (`000a`) / OFF (`003e`)

Temperature is communicated in **Fahrenheit**; all scripts convert to °C internally.

### Brooks 0250 MFC (ASCII)

```python
# Enable serial control
AZ.{output_port}P46=1

# Set flow (mL/min)
AZ.{output_port}P01={flow:.2f}

# Read actual flow
AZ.{input_port}K        # response is comma-separated; field index 6

# Read setpoint
AZ.{output_port}P01?    # response is comma-separated; field index 4
```

---

## Installation

```bash
pip install -r requirements.txt
```

---

## Repository Structure

```
lab-automation/
├── experiments/          # Reactor experiment scripts
│   ├── stability_test.py       # Ramp → 375°C, 3-hour hold, cool
│   ├── multi_ramp.py           # Two-stage ramp (200°C then 600°C)
│   ├── ramp_hold.py            # Simple ramp + hold, no MFC control
│   ├── full_kinetics.py        # NH₃ + H₂ kinetics combined
│   ├── reduction_experiment.py # Reduction protocol
│   └── temperature_ramp.py     # Basic temperature ramp utility
├── utils/
│   ├── heartbeat.py            # Shared crash-detection heartbeat module
│   └── mfc_reader.py           # Read actual/setpoint of all 4 MFC channels
├── tools/
│   ├── ai2_plotter.py          # Real-time live temperature plotter (AI1 + AI2)
│   ├── watchdog.py             # Safety watchdog — shuts heater if experiment crashes
│   └── scheduler.py            # Run any script at a specified time (e.g. midnight)
├── requirements.txt
├── .gitignore
└── README.md
```

---

## Experiment Scripts

### `experiments/stability_test.py`

Stability/reduction test at 375°C.

**Stages:**
1. **RAMP** — 25°C → 375°C at 10°C/min; CH3=20, CH4=4
2. **HOLD** — 3 hours at 375°C; CH1=20, CH4=4 (CH2, CH3 = 0)
3. **COOL** — Control loop OFF, wait 30 min; CH1=20, CH4=4
4. **END** — CH3=20, CH4=4 until Ctrl+C

**Configure at the top of the file:**
```python
COM_WATLOW = 'COM4'
COM_MFC    = 'COM2'
TARGET_TEMP    = 375
RAMP_RATE      = 10      # °C/min
HOLD_DURATION  = 180     # minutes
AI2_SAFETY_LIMIT = 90   # °C — emergency shutdown threshold
```

---

### `experiments/multi_ramp.py`

Two-stage ramp for activation/kinetics protocols.

**Stages:**
1. **RAMP1** — → 200°C at 10°C/min; CH3=20, CH4=4
2. **HOLD1** — 15 min at 200°C; CH1=20, CH4=4
3. **RAMP2** — → 600°C at 1°C/min; CH1=20, CH4=4
4. **HOLD2** — 15 min at 600°C; CH1=20, CH4=4
5. **COOL** — Heater OFF, 20 min; CH1=20, CH4=4
6. **END** — CH3=20, CH4=4

---

### `experiments/ramp_hold.py`

Minimal ramp + hold with no MFC control. Good for catalyst sintering, annealing, or temperature-only tests.

**Configure:**
```python
TARGET_TEMP     = 500    # °C
RAMP_RATE       = 10     # °C/min
HOLD_DURATION   = 180    # minutes
AI2_SAFETY_LIMIT = 90   # °C
```

Logs: `ramp_hold_YYYYMMDD_HHMMSS.csv`

---

### `experiments/full_kinetics.py`

Full kinetics protocol combining NH₃ decomposition and H₂ production steps.

**Stages:**
1. **RAMP** — → 550°C at 10°C/min; CH3=24
2. **NH3_KIN** — 5 steps × 30 min (CH1: 20→12, CH4: 4→12)
3. **PURGE1** — 15 min; CH3=24
4. **H2_KIN** — 7 steps × 30 min (CH1=20 fixed, CH3/CH4 vary per `H2_STEPS`)
5. **COOL** — Heater OFF, 30 min; CH1=20, CH4=4
6. **END** — CH3=24

---

## Utility Scripts

### `utils/mfc_reader.py`

Polls all 4 MFC channels every 2 seconds and prints actual flow vs setpoint.

```bash
python utils/mfc_reader.py
```

Useful for verifying MFC communication before running an experiment.

---

## Tools

### `tools/ai2_plotter.py`

Real-time live plot of AI1 (bed) and AI2 (surface) temperatures. Read-only — does **not** control the heater.

- Dark-theme dashboard
- Main plot: last 30 minutes, both thermocouples
- Zoom plot: AI2 last 5 minutes, tight autoscale (±0.05°C minimum)
- Live readouts to 3 decimal places
- Stats: Min, Max, Avg, Δ spread
- Logs to CSV with 4 decimal places

```bash
python tools/ai2_plotter.py
```

Can also be used standalone to monitor lab temperature fluctuations (e.g. door open/close effects) without running any experiment.

---

### `tools/watchdog.py`

Run in a **separate terminal** alongside any experiment. Monitors `experiment_heartbeat.txt` and triggers an emergency shutdown if the experiment script stops updating it (crash, hang, or serial error).

```bash
# Terminal 1
python experiments/full_kinetics.py

# Terminal 2
python tools/watchdog.py
```

**How it works:**
- Experiment scripts call `write_heartbeat()` before every `time.sleep()`
- Watchdog checks heartbeat every 10 seconds
- If heartbeat is older than 30 seconds → sets setpoint to 25°C and turns control loop OFF
- Retries shutdown every 5 seconds until confirmed

> ⚠️ **Limitation:** This only protects against software crashes. A full PC power failure will leave the Watlow at its last setpoint. For true hardware-level protection, configure the Watlow's built-in **idle timeout** register or use an external UPS with a shutdown script.

---

### `tools/scheduler.py`

Launches any experiment script at a specified time.

```python
# Edit these lines in scheduler.py:
SCRIPT_TO_RUN = 'experiments/full_kinetics.py'
RUN_HOUR   = 0   # 24-hour format (0 = midnight)
RUN_MINUTE = 0
RUN_SECOND = 0
```

```bash
python tools/scheduler.py
```

Shows a live countdown. If the specified time has already passed today, it schedules for the same time tomorrow.

---

## Crash Protection & Safety

All experiment scripts use the shared heartbeat module:

```python
from utils.heartbeat import write_heartbeat, clear_heartbeat

# In every loop:
write_heartbeat()
time.sleep(interval)

# In finally block:
clear_heartbeat()
```

Safety limit (`AI2_SAFETY_LIMIT`) is checked every loop iteration. If AI2 exceeds the limit, the control loop is immediately switched OFF.

---

## CSV Logging

All experiment scripts log to CSV with columns:

```
timestamp, elapsed_min, setpoint_C, AI1_bed_C, AI2_surface_C, stage
```

Files are named with a datetime stamp, e.g. `stability_test_20260101_000000.csv`.

---

## License

MIT — see [LICENSE](LICENSE) for details.
