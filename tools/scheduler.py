"""
Experiment Scheduler
---------------------
Select a Python script and schedule it to run at a specific time.
The scheduler waits until the target time then launches the script
as a subprocess — so this window just acts as a timer/launcher.

Usage:
    python scheduler.py
"""

import subprocess
import time
import os
from datetime import datetime, timedelta

# ═══════════════════════════════════════════════════════════════
#  SETTINGS — edit these
# ═══════════════════════════════════════════════════════════════

# Full path to the script you want to run, or just filename if
# it's in the same folder as this scheduler
SCRIPT_TO_RUN = 'full_kinetics.py'

# Time to run — 24-hour format
RUN_HOUR   = 0    # 0 = midnight, 12 = noon, 23 = 11pm
RUN_MINUTE = 0
RUN_SECOND = 0

# ═══════════════════════════════════════════════════════════════


def get_next_run_time(hour, minute, second):
    """Return the next datetime matching the given time (today or tomorrow)."""
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if target <= now:
        # Time has already passed today — schedule for tomorrow
        target += timedelta(days=1)
    return target


def countdown_to(target_time):
    """Print a live countdown until target_time."""
    while True:
        now       = datetime.now()
        remaining = (target_time - now).total_seconds()

        if remaining <= 0:
            print("\n  ✓ Time reached — launching script...")
            break

        hours, rem = divmod(int(remaining), 3600)
        mins,  secs = divmod(rem, 60)
        print(f"  Launching in: {hours:02d}:{mins:02d}:{secs:02d}    ", end='\r')
        time.sleep(1)


def main():
    # Resolve script path
    script_path = os.path.abspath(SCRIPT_TO_RUN)

    if not os.path.exists(script_path):
        print(f"\n  ERROR: Script not found: {script_path}")
        print("  Check SCRIPT_TO_RUN in the settings section.")
        return

    target_time = get_next_run_time(RUN_HOUR, RUN_MINUTE, RUN_SECOND)

    # Format time nicely
    time_str = target_time.strftime('%Y-%m-%d %H:%M:%S')
    ampm_str = target_time.strftime('%I:%M:%S %p')

    print()
    print("=" * 65)
    print("  EXPERIMENT SCHEDULER")
    print("=" * 65)
    print(f"  Script         : {os.path.basename(script_path)}")
    print(f"  Scheduled for  : {time_str}  ({ampm_str})")
    print(f"  Current time   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)
    print("  Press Ctrl+C to cancel")
    print("=" * 65)
    print()

    try:
        countdown_to(target_time)
    except KeyboardInterrupt:
        print("\n\n  Scheduler cancelled by user.")
        return

    # Launch the script
    print(f"\n  Launching: {script_path}")
    print(f"  Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    print("=" * 65)
    print("  SCRIPT OUTPUT")
    print("=" * 65)

    try:
        # Run script — output streams directly to this console
        result = subprocess.run(
            ['python', script_path],
            check=False   # don't raise on non-zero exit
        )

        print()
        print("=" * 65)
        if result.returncode == 0:
            print("  Script finished successfully.")
        else:
            print(f"  Script exited with code {result.returncode}.")
        print("=" * 65)

    except FileNotFoundError:
        print("  ERROR: python interpreter not found.")
        print("  Try changing 'python' to 'python3' in the subprocess.run call.")
    except KeyboardInterrupt:
        print("\n  Script interrupted by user.")


if __name__ == '__main__':
    main()
