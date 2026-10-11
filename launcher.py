#!/usr/bin/env python3
"""Let the controller finish cleanup when Quickshell kills its Process wrapper."""
from pathlib import Path
import signal
import subprocess
import sys

child = subprocess.Popen([sys.executable, '-u', str(Path(__file__).with_name('controller.py'))])


def stop(signum, frame):
    if child.poll() is None:
        child.send_signal(signal.SIGTERM)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
sys.exit(child.wait())
