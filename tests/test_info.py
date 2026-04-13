#!/usr/bin/env python3
"""
test_info.py -- Tests for the info command (device and file).

Device tests cross-validate against real picotool: both tools read
binary_info from the same device, and the extracted program_name must
match. File tests use info_file() on synthetic buffers.

Each device test opens/closes its own Picotool connection so real
picotool can access the device for cross-validation.

Requires for device tests:
  - Pico in BOOTSEL mode
  - Real picotool in PATH
"""

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from _harness import (
    TestResults, build_binary_info_buffer, has_bootsel_device,
    has_real_picotool, run_tool, SKIP,
)
from _binary_info import (
    BINARY_INFO_ID_RP_PROGRAM_NAME,
    BINARY_INFO_ID_RP_PROGRAM_VERSION_STRING,
)
from _uf2 import create_uf2, RP2350_ARM_S_FAMILY_ID
from picotool_lib import Picotool, PicotoolError


# ---------------------------------------------------------------------------
#  Device tests
# ---------------------------------------------------------------------------

def _real_picotool_program_name():
    """Extract program name from real picotool info output."""
    r = run_tool(['picotool', 'info'], check=False)
    for line in r.stdout.splitlines():
        m = re.match(r'\s*name:\s+(.+)', line)
        if m:
            return m.group(1).strip()
    return None


def test_info_device_cross_validate():
    """info() program_name matches what real picotool reports.

    If neither tool finds binary_info, that's consistent (not a failure).
    If only one finds it, that's a bug."""
    with Picotool() as pt:
        ours = pt.info()
    our_name = ours.program_name if ours else None
    # Connection closed -- real picotool can claim the device
    their_name = _real_picotool_program_name()
    if our_name is None and their_name is None:
        return None  # both agree: no binary_info
    if our_name != their_name:
        return 'name mismatch: ours=%r real=%r' % (our_name, their_name)
    return None


def test_info_returns_binary_info():
    """info() returns a BinaryInfo object on a typical device with firmware."""
    with Picotool() as pt:
        info = pt.info()
    if info is not None and info.program_name is None:
        return 'BinaryInfo returned but program_name is None'
    return None


# ---------------------------------------------------------------------------
#  File tests (offline, no device needed)
# ---------------------------------------------------------------------------

def test_info_file_bin():
    """info_file() extracts program_name from a synthetic BIN file."""
    buf = build_binary_info_buffer([
        {'type': 'string', 'id': BINARY_INFO_ID_RP_PROGRAM_NAME,
         'value': 'file_test_app'},
        {'type': 'string', 'id': BINARY_INFO_ID_RP_PROGRAM_VERSION_STRING,
         'value': '3.0.0'},
    ])
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        f.write(buf)
        path = f.name
    try:
        pt = Picotool()
        info = pt.info_file(path)
        if info is None:
            return 'info_file returned None for valid BIN'
        if info.program_name != 'file_test_app':
            return 'name: expected "file_test_app", got %r' % info.program_name
        if info.program_version != '3.0.0':
            return 'version: expected "3.0.0", got %r' % info.program_version
    finally:
        os.unlink(path)
    return None


def test_info_file_uf2():
    """info_file() extracts program_name from a UF2 file wrapping
    a synthetic binary_info buffer."""
    buf = build_binary_info_buffer([
        {'type': 'string', 'id': BINARY_INFO_ID_RP_PROGRAM_NAME,
         'value': 'uf2_test_app'},
    ])
    uf2_data = create_uf2(buf, 0x10000000, RP2350_ARM_S_FAMILY_ID)
    with tempfile.NamedTemporaryFile(suffix='.uf2', delete=False) as f:
        f.write(uf2_data)
        path = f.name
    try:
        pt = Picotool()
        info = pt.info_file(path)
        if info is None:
            return 'info_file returned None for valid UF2'
        if info.program_name != 'uf2_test_app':
            return 'name: expected "uf2_test_app", got %r' % info.program_name
    finally:
        os.unlink(path)
    return None


def test_info_file_missing():
    """info_file() raises PicotoolError for nonexistent file."""
    pt = Picotool()
    try:
        pt.info_file('/nonexistent/path/firmware.bin')
        return 'should have raised PicotoolError'
    except PicotoolError:
        pass
    return None


# ---------------------------------------------------------------------------
#  Runner
# ---------------------------------------------------------------------------

def main():
    r = TestResults('test_info')
    print('[test_info] ', end='')

    # File tests always run (offline)
    r.run_test('info_file_bin', test_info_file_bin)
    r.run_test('info_file_uf2', test_info_file_uf2)
    r.run_test('info_file_missing', test_info_file_missing)

    # Device tests require hardware + real picotool
    if has_bootsel_device() and has_real_picotool():
        r.run_test('info_device_cross_validate', test_info_device_cross_validate)
        r.run_test('info_returns_binary_info', test_info_returns_binary_info)
    else:
        r.record('info_device_cross_validate', SKIP, 'no device or picotool')
        r.record('info_returns_binary_info', SKIP, 'no device or picotool')

    return r.summary()


if __name__ == '__main__':
    sys.exit(main())
