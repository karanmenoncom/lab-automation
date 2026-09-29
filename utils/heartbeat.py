"""
heartbeat.py
-------------
Shared heartbeat helper used by all experiment scripts.
The watchdog reads this file to check if the experiment is still alive.

Usage in experiment scripts:
    from heartbeat import write_heartbeat, clear_heartbeat
    write_heartbeat()   # call every loop iteration
    clear_heartbeat()   # call in finally block when experiment ends cleanly
"""

import time
import os

HEARTBEAT_FILE = 'experiment_heartbeat.txt'

def write_heartbeat():
    """Write current timestamp to heartbeat file."""
    try:
        with open(HEARTBEAT_FILE, 'w') as f:
            f.write(str(time.time()))
    except Exception:
        pass   # never crash the experiment due to heartbeat failure

def clear_heartbeat():
    """Delete heartbeat file on clean exit so watchdog knows it ended normally."""
    try:
        if os.path.exists(HEARTBEAT_FILE):
            os.remove(HEARTBEAT_FILE)
    except Exception:
        pass
