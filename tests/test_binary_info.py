#!/usr/bin/env python3
"""
test_binary_info.py -- Offline tests for the _binary_info module.

Tests parse_binary_info() against synthetically constructed flash
buffers with known binary_info headers and entries. Each buffer is
built byte-by-byte from the struct definitions in pico-sdk
binary_info/structure.h, so a bug in our parser can't hide behind
a symmetric construction/parsing pair.

No hardware required. No C++ picotool required.
"""

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from _harness import TestResults, build_binary_info_buffer
from _binary_info import (
    BINARY_INFO_ID_RP_BINARY_END,
    BINARY_INFO_ID_RP_PICO_BOARD,
    BINARY_INFO_ID_RP_PROGRAM_BUILD_DATE_STRING,
    BINARY_INFO_ID_RP_PROGRAM_DESCRIPTION,
    BINARY_INFO_ID_RP_PROGRAM_FEATURE,
    BINARY_INFO_ID_RP_PROGRAM_NAME,
    BINARY_INFO_ID_RP_PROGRAM_VERSION_STRING,
    BINARY_INFO_ID_RP_SDK_VERSION,
    BINARY_INFO_MARKER_END,
    BINARY_INFO_MARKER_START,
    parse_binary_info,
)


# ---------------------------------------------------------------------------
#  Tests
# ---------------------------------------------------------------------------

def test_program_name():
    """Parser extracts PROGRAM_NAME from a minimal binary_info buffer."""
    buf = build_binary_info_buffer([
        {'type': 'string', 'id': BINARY_INFO_ID_RP_PROGRAM_NAME,
         'value': 'test_app'},
    ])
    info = parse_binary_info(buf, 0x10000000, 'rp2350')
    if info is None:
        return 'parse returned None'
    if info.program_name != 'test_app':
        return 'expected "test_app", got %r' % info.program_name
    return None


def test_multiple_fields():
    """Parser extracts all standard string fields from one buffer."""
    buf = build_binary_info_buffer([
        {'type': 'string', 'id': BINARY_INFO_ID_RP_PROGRAM_NAME,
         'value': 'my_firmware'},
        {'type': 'string', 'id': BINARY_INFO_ID_RP_PROGRAM_VERSION_STRING,
         'value': '1.2.3'},
        {'type': 'string', 'id': BINARY_INFO_ID_RP_PROGRAM_BUILD_DATE_STRING,
         'value': 'Apr 13 2026'},
        {'type': 'string', 'id': BINARY_INFO_ID_RP_SDK_VERSION,
         'value': '2.1.0'},
        {'type': 'string', 'id': BINARY_INFO_ID_RP_PICO_BOARD,
         'value': 'pico2'},
        {'type': 'string', 'id': BINARY_INFO_ID_RP_PROGRAM_DESCRIPTION,
         'value': 'A test program'},
    ])
    info = parse_binary_info(buf, 0x10000000, 'rp2350')
    if info is None:
        return 'parse returned None'
    checks = [
        ('program_name', info.program_name, 'my_firmware'),
        ('program_version', info.program_version, '1.2.3'),
        ('program_build_date', info.program_build_date, 'Apr 13 2026'),
        ('sdk_version', info.sdk_version, '2.1.0'),
        ('pico_board', info.pico_board, 'pico2'),
        ('program_description', info.program_description, 'A test program'),
    ]
    for field, got, expected in checks:
        if got != expected:
            return '%s: expected %r, got %r' % (field, expected, got)
    return None


def test_binary_end_int():
    """Parser extracts BINARY_END from an ID_AND_INT entry."""
    buf = build_binary_info_buffer([
        {'type': 'int', 'id': BINARY_INFO_ID_RP_BINARY_END,
         'value': 0x10040000},
    ])
    info = parse_binary_info(buf, 0x10000000, 'rp2350')
    if info is None:
        return 'parse returned None'
    if info.binary_end != 0x10040000:
        return 'expected 0x10040000, got 0x%08x' % info.binary_end
    return None


def test_feature_list():
    """Multiple PROGRAM_FEATURE entries accumulate into a list."""
    buf = build_binary_info_buffer([
        {'type': 'string', 'id': BINARY_INFO_ID_RP_PROGRAM_FEATURE,
         'value': 'USB'},
        {'type': 'string', 'id': BINARY_INFO_ID_RP_PROGRAM_FEATURE,
         'value': 'UART'},
    ])
    info = parse_binary_info(buf, 0x10000000, 'rp2350')
    if info is None:
        return 'parse returned None'
    if info.program_features != ['USB', 'UART']:
        return 'expected [USB, UART], got %r' % info.program_features
    return None


def test_rp2040_scan_offset():
    """RP2040 header at offset 0x100 (after boot2) is found correctly.

    pico-sdk defs.h: for RP2040, scan starts at base+0x100 with a
    64-byte window. Our builder places the header at 0x100 when
    family='rp2040' and base_addr=0x10000000."""
    buf = build_binary_info_buffer([
        {'type': 'string', 'id': BINARY_INFO_ID_RP_PROGRAM_NAME,
         'value': 'rp2040_app'},
    ], base_addr=0x10000000, family='rp2040')
    info = parse_binary_info(buf, 0x10000000, 'rp2040')
    if info is None:
        return 'parse returned None for rp2040 buffer'
    if info.program_name != 'rp2040_app':
        return 'expected "rp2040_app", got %r' % info.program_name
    return None


def test_no_header_returns_none():
    """Buffer with no binary_info markers returns None, not an error."""
    buf = b'\xFF' * 4096
    info = parse_binary_info(buf, 0x10000000, 'rp2350')
    if info is not None:
        return 'expected None for blank buffer, got %r' % info
    return None


def test_marker_constants():
    """Verify our marker constants match the SDK definitions.
    Catches copy-paste errors in the constant declarations."""
    # Values from pico-sdk binary_info/defs.h:40-41
    if BINARY_INFO_MARKER_START != 0x7188EBF2:
        return 'MARKER_START mismatch'
    if BINARY_INFO_MARKER_END != 0xE71AA390:
        return 'MARKER_END mismatch'
    return None


# ---------------------------------------------------------------------------
#  Runner
# ---------------------------------------------------------------------------

def main():
    r = TestResults('test_binary_info')
    print('[test_binary_info] ', end='')

    r.run_test('program_name', test_program_name)
    r.run_test('multiple_fields', test_multiple_fields)
    r.run_test('binary_end_int', test_binary_end_int)
    r.run_test('feature_list', test_feature_list)
    r.run_test('rp2040_scan_offset', test_rp2040_scan_offset)
    r.run_test('no_header_returns_none', test_no_header_returns_none)
    r.run_test('marker_constants', test_marker_constants)

    return r.summary()


if __name__ == '__main__':
    sys.exit(main())
