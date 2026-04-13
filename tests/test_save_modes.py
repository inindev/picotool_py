#!/usr/bin/env python3
"""
test_save_modes.py -- Hardware tests for save --range/--program/--all,
BIN and UF2 output formats.

Each test is self-contained: opens its own Picotool connection for the
API call, closes it, then uses real picotool to cross-validate the
saved file contents.

Requires:
  - Pico in BOOTSEL mode
  - Real picotool in PATH
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from _harness import (
    TestResults, FlashRegion, first_diff, get_test_region,
    has_bootsel_device, has_real_picotool, make_pattern,
    tool_load, tool_save, SKIP,
)
from _uf2 import parse_uf2, RP2040_FAMILY_ID
from picotool_lib import Picotool, PicotoolError

SECTOR = 0x1000


# ---------------------------------------------------------------------------
#  Tests
# ---------------------------------------------------------------------------

def test_save_range_bin(base):
    """save() range to BIN: compare our file against real picotool readback.

    Write a known pattern via real picotool, save range via our API,
    read the same range via real picotool, compare byte-for-byte."""
    addr, size = base, SECTOR
    tool_load(addr, make_pattern(size, seed=0xA1), real=True)

    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        path = f.name
    try:
        with Picotool() as pt:
            pt.save(addr, size, path)
        with open(path, 'rb') as f:
            our_data = f.read()
        ref_data = tool_save(addr, size, real=True)
        d = first_diff(our_data, ref_data)
        if d is not None:
            return 'BIN save mismatch at offset 0x%x' % d[0]
    finally:
        os.unlink(path)
    return None


def test_save_range_uf2(base):
    """save() range to UF2: parse the UF2 payload, compare against real
    picotool readback of the same flash range."""
    addr, size = base + 0x1000, SECTOR
    tool_load(addr, make_pattern(size, seed=0xA2), real=True)

    with tempfile.NamedTemporaryFile(suffix='.uf2', delete=False) as f:
        path = f.name
    try:
        with Picotool() as pt:
            pt.save(addr, size, path)
        fam, img, start, end = parse_uf2(path)
        ref_data = tool_save(addr, size, real=True)
        # UF2 may be page-aligned; extract the exact range we saved
        offset = addr - start
        our_data = bytes(img[offset:offset + size])
        d = first_diff(our_data, ref_data)
        if d is not None:
            return 'UF2 payload mismatch at offset 0x%x' % d[0]
    finally:
        os.unlink(path)
    return None


def test_save_all_size():
    """save_all() produces data matching flash size, spot-checked against
    real picotool."""
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        path = f.name
    try:
        with Picotool() as pt:
            pt.save_all(path)
            expected_size = pt.guess_flash_size()
        with open(path, 'rb') as f:
            data = f.read()
        if len(data) != expected_size:
            return 'size mismatch: file=%d, guess_flash_size=%d' % (
                len(data), expected_size)
        # Spot-check first 256 bytes against real picotool
        ref = tool_save(0x10000000, 256, real=True)
        d = first_diff(data[:256], ref)
        if d is not None:
            return 'first-page spot-check mismatch at offset 0x%x' % d[0]
    finally:
        os.unlink(path)
    return None


def test_save_program():
    """save_program() extent from binary_info, cross-checked against
    real picotool. Skips gracefully if device has no binary_info."""
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        path = f.name
    try:
        with Picotool() as pt:
            try:
                pt.save_program(path)
            except PicotoolError as e:
                if 'Cannot determine' in str(e):
                    return None  # no binary_info; not a failure
                raise
        with open(path, 'rb') as f:
            data = f.read()
        if len(data) == 0:
            return 'save_program produced empty file'
        ref = tool_save(0x10000000, min(256, len(data)), real=True)
        d = first_diff(data[:len(ref)], ref)
        if d is not None:
            return 'first-page mismatch at offset 0x%x' % d[0]
    finally:
        os.unlink(path)
    return None


def test_save_family_override(base):
    """save with explicit family_id writes that family into the UF2 header."""
    addr, size = base + 0x2000, SECTOR
    tool_load(addr, make_pattern(size, seed=0xA5), real=True)

    with tempfile.NamedTemporaryFile(suffix='.uf2', delete=False) as f:
        path = f.name
    try:
        with Picotool() as pt:
            pt._save_range(addr, size, path, 'uf2', RP2040_FAMILY_ID, None)
        fam, _, _, _ = parse_uf2(path, valid_families={RP2040_FAMILY_ID})
        if fam != RP2040_FAMILY_ID:
            return 'forced family 0x%08x but got 0x%08x' % (
                RP2040_FAMILY_ID, fam)
    finally:
        os.unlink(path)
    return None


# ---------------------------------------------------------------------------
#  Runner
# ---------------------------------------------------------------------------

def main():
    r = TestResults('test_save_modes')

    if not has_bootsel_device():
        print('[test_save_modes] SKIP (no BOOTSEL device)')
        r.record('all', SKIP, 'no BOOTSEL device')
        return r.summary()
    if not has_real_picotool():
        print('[test_save_modes] SKIP (real picotool not in PATH)')
        r.record('all', SKIP, 'real picotool not in PATH')
        return r.summary()

    test_base, test_size = get_test_region()
    print('[test_save_modes] (region 0x%08x, %d KB) ' % (test_base, test_size // 1024), end='')

    with FlashRegion(test_base, test_size):
        r.run_test('save_range_bin', lambda: test_save_range_bin(test_base))
        r.run_test('save_range_uf2', lambda: test_save_range_uf2(test_base))
        r.run_test('save_all_size', test_save_all_size)
        r.run_test('save_program', test_save_program)
        r.run_test('save_family_override', lambda: test_save_family_override(test_base))

    return r.summary()


if __name__ == '__main__':
    sys.exit(main())
