#!/usr/bin/env python3
"""
test_reboot.py -- Hardware test for reboot to application mode.

DESTRUCTIVE: the device leaves BOOTSEL after this test. Run last.

Methodology: confirm device is in BOOTSEL, call reboot(), poll USB
for up to 3 seconds to confirm the device disappears. If it's still
in BOOTSEL after the deadline, the reboot didn't take effect.

Requires:
  - Pico in BOOTSEL mode
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from _harness import TestResults, has_bootsel_device, SKIP
from picotool_lib import Picotool


def test_reboot_app():
    """reboot() causes device to leave BOOTSEL within 3 seconds.

    The only reliable observable is USB enumeration: if the device
    is no longer visible as a BOOTSEL device, it rebooted into the
    application (or at minimum left the bootloader)."""
    with Picotool() as pt:
        pt.reboot()

    # Poll for disappearance
    deadline = 3.0
    interval = 0.2
    start = time.time()
    while time.time() - start < deadline:
        if not has_bootsel_device():
            return None  # PASS: device left BOOTSEL
        time.sleep(interval)

    return 'device still in BOOTSEL %.1fs after reboot' % deadline


# ---------------------------------------------------------------------------
#  Runner
# ---------------------------------------------------------------------------

def main():
    r = TestResults('test_reboot')

    if not has_bootsel_device():
        print('[test_reboot] SKIP (no BOOTSEL device)')
        r.record('reboot_app', SKIP, 'no BOOTSEL device')
        return r.summary()

    print('[test_reboot] ', end='')
    r.run_test('reboot_app', test_reboot_app)
    return r.summary()


if __name__ == '__main__':
    sys.exit(main())
