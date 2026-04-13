#!/usr/bin/env python3
"""
test_reboot.py -- Hardware tests for reboot, load --execute, and --force.

DESTRUCTIVE: the device changes USB state during these tests. Run last.

Test sequence (order matters -- each test sets up state for the next):
  1. force_error:    device in BOOTSEL -> force should fail cleanly
  2. reboot_app:     device in BOOTSEL -> reboot -> device running app
  -- if firmware links pico_stdio_usb: --
  3. force_bootsel:  device running app -> force into BOOTSEL
  4. load_execute:   device in BOOTSEL -> load + execute -> device running app
  5. force_bootsel2: device running app -> force into BOOTSEL (round-trip)

If the firmware doesn't expose stdio_usb, tests 3-5 are skipped (not
failed) since --force requires the USB reset interface.

Requires:
  - Pico in BOOTSEL mode (to start)
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from _harness import (
    TestResults, has_bootsel_device, has_stdio_usb_device, SKIP,
)
from picotool_lib import Picotool, PicotoolError


def _wait_for_bootsel(timeout=5.0):
    """Poll until device appears in BOOTSEL mode. Returns True if found."""
    start = time.time()
    while time.time() - start < timeout:
        if has_bootsel_device():
            return True
        time.sleep(0.3)
    return False


def _wait_for_no_bootsel(timeout=3.0):
    """Poll until device disappears from BOOTSEL. Returns True if gone."""
    start = time.time()
    while time.time() - start < timeout:
        if not has_bootsel_device():
            return True
        time.sleep(0.2)
    return False


def _wait_for_stdio_usb(timeout=5.0):
    """Poll until a stdio_usb device appears. Returns True if found."""
    start = time.time()
    while time.time() - start < timeout:
        if has_stdio_usb_device():
            return True
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
#  Tests
# ---------------------------------------------------------------------------

def test_force_error_no_device():
    """force_into_bootsel() raises cleanly when no stdio_usb device exists.

    Device is in BOOTSEL, so no stdio_usb device is present."""
    pt = Picotool()
    try:
        pt.force_into_bootsel(wait=False)
        return 'should have raised when no stdio_usb device present'
    except Exception:
        pass  # expected
    return None


def test_reboot_app():
    """reboot() causes device to leave BOOTSEL.

    Pre:  device in BOOTSEL
    Post: device running application"""
    with Picotool() as pt:
        pt.reboot()
    if not _wait_for_no_bootsel():
        return 'device still in BOOTSEL after reboot'
    return None


def test_force_bootsel():
    """force_into_bootsel() brings a running device back to BOOTSEL.

    Pre:  device running application (with pico_stdio_usb)
    Post: device in BOOTSEL"""
    pt = Picotool()
    try:
        pt.force_into_bootsel(wait=True)
    except Exception as e:
        return 'force_into_bootsel raised: %s' % e
    if not _wait_for_bootsel():
        return 'device did not appear in BOOTSEL after force reboot'
    return None


def test_load_execute():
    """load(execute=True) writes firmware then reboots into it.

    Pre:  device in BOOTSEL
    Post: device running application

    Re-loads the same firmware already on the device so state is
    unchanged. Validates that execute causes device to leave BOOTSEL."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        path = f.name
    try:
        with Picotool() as pt:
            info = pt.info()
            if info is None or info.binary_end == 0:
                return None  # can't determine firmware extent; skip
            size = info.binary_end - 0x10000000
            pt.save(0x10000000, size, path)

        with Picotool() as pt:
            pt.load(path, offset=0x10000000, execute=True)
    finally:
        os.unlink(path)

    if not _wait_for_no_bootsel():
        return 'device still in BOOTSEL after load --execute'
    return None


def test_force_bootsel_again():
    """Second force_into_bootsel() confirms the first wasn't a fluke.

    Pre:  device running application
    Post: device in BOOTSEL"""
    pt = Picotool()
    try:
        pt.force_into_bootsel(wait=True)
    except Exception as e:
        return 'force_into_bootsel raised: %s' % e
    if not _wait_for_bootsel():
        return 'device did not appear in BOOTSEL after second force reboot'
    return None


# ---------------------------------------------------------------------------
#  Runner
# ---------------------------------------------------------------------------

def main():
    r = TestResults('test_reboot')

    if not has_bootsel_device():
        print('[test_reboot] SKIP (no BOOTSEL device)')
        r.record('all', SKIP, 'no BOOTSEL device')
        return r.summary()

    print('[test_reboot] ', end='')

    # Phase 1: error path (device stays in BOOTSEL)
    r.run_test('force_error_no_device', test_force_error_no_device)

    # Phase 2: reboot to app (device leaves BOOTSEL)
    r.run_test('reboot_app', test_reboot_app)

    # Phase 3: force/execute round-trip (requires pico_stdio_usb).
    # Wait to see if the running firmware exposes stdio_usb.
    has_stdio = _wait_for_stdio_usb(timeout=5.0)

    if has_stdio:
        r.run_test('force_bootsel', test_force_bootsel)
        r.run_test('load_execute', test_load_execute)
        # After load_execute, device is running again; wait for stdio_usb
        if _wait_for_stdio_usb(timeout=5.0):
            r.run_test('force_bootsel_again', test_force_bootsel_again)
        else:
            r.record('force_bootsel_again', SKIP, 'no stdio_usb after execute')
    else:
        r.record('force_bootsel', SKIP, 'firmware does not link pico_stdio_usb')
        r.record('load_execute', SKIP, 'firmware does not link pico_stdio_usb')
        r.record('force_bootsel_again', SKIP, 'firmware does not link pico_stdio_usb')

    return r.summary()


if __name__ == '__main__':
    sys.exit(main())
