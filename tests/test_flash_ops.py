#!/usr/bin/env python3
"""
test_flash_ops.py -- Hardware tests for read, write, erase, verify.

Methodology: each test is self-contained. It opens its own Picotool
connection for the API operation, closes it, then calls real picotool
(which needs exclusive USB access) to cross-validate the result.
This interleaving ensures neither tool's bugs can mask the other's.

The test region is the last 64 KB of actual flash, detected at startup
via guess_flash_size(). This adapts to any board (2 MB, 4 MB, 16 MB).

Requires:
  - Pico in BOOTSEL mode
  - Real picotool in PATH (ground truth for cross-validation)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from _harness import (
    TestResults, FlashRegion, first_diff, get_test_region,
    has_bootsel_device, has_real_picotool, is_all_ff, make_pattern,
    tool_save, tool_load, tool_erase, PASS, SKIP,
)
from picotool_lib import Picotool, PicotoolError

SECTOR = 0x1000    # 4 KB


# ---------------------------------------------------------------------------
#  Tests -- each opens/closes its own connection around API calls,
#  leaving the device free for real picotool between operations.
#  `base` is the start of the test region (end of flash minus 64 KB).
# ---------------------------------------------------------------------------

def test_read_cross_validate(base):
    """Read 4 KB via our API, compare byte-for-byte against real picotool.

    Both tools read the same flash range; the bytes must be identical.
    Any difference indicates a protocol or alignment bug."""
    addr, size = base, SECTOR
    with Picotool() as pt:
        ours = pt.read(addr, size)
    theirs = tool_save(addr, size, real=True)
    d = first_diff(ours, theirs)
    if d is not None:
        return 'byte mismatch at offset 0x%x: ours=0x%02x real=0x%02x' % d
    return None


def test_write_read_roundtrip(base):
    """Write pattern via our API, read back via our API, compare.

    Not a cross-validation (both sides are ours), but catches internal
    write corruption that a cross-tool test might attribute to the
    wrong side."""
    addr, size = base + 0x1000, SECTOR
    pattern = make_pattern(size, seed=0x11)
    with Picotool() as pt:
        pt.write(addr, pattern)
        readback = pt.read(addr, size)
    d = first_diff(pattern, readback)
    if d is not None:
        return 'roundtrip mismatch at offset 0x%x' % d[0]
    return None


def test_write_cross_validate(base):
    """Write via our API, read back via real picotool, compare.

    The authoritative test: our written data must match what the
    C++ reference reads from the same flash address."""
    addr, size = base + 0x2000, SECTOR
    pattern = make_pattern(size, seed=0x22)
    with Picotool() as pt:
        pt.write(addr, pattern)
    # Connection closed -- real picotool can now claim the device
    theirs = tool_save(addr, size, real=True)
    d = first_diff(pattern, theirs)
    if d is not None:
        return 'cross-tool mismatch at offset 0x%x: expected=0x%02x real=0x%02x' % d
    return None


def test_real_write_our_read(base):
    """Write via real picotool, read via our API, compare.

    The inverse of test_write_cross_validate. Together they prove
    both directions of the read/write path are byte-accurate."""
    addr, size = base + 0x3000, SECTOR
    pattern = make_pattern(size, seed=0x33)
    # Real picotool writes first (no connection held)
    tool_load(addr, pattern, real=True)
    with Picotool() as pt:
        ours = pt.read(addr, size)
    d = first_diff(pattern, ours)
    if d is not None:
        return 'cross-tool mismatch at offset 0x%x: expected=0x%02x ours=0x%02x' % d
    return None


def test_erase_cross_validate(base):
    """Write pattern, erase via our API, confirm all 0xFF via real picotool.

    The erase must produce the same result the C++ tool observes."""
    addr, size = base + 0x4000, SECTOR
    tool_load(addr, make_pattern(size, seed=0x44), real=True)
    with Picotool() as pt:
        pt.erase(addr, size)
    theirs = tool_save(addr, size, real=True)
    if not is_all_ff(theirs):
        n = sum(b != 0xFF for b in theirs)
        return '%d non-FF bytes after erase (via real picotool)' % n
    return None


def test_erase_sector_alignment(base):
    """Erase unaligned range, confirm it rounds outward to sectors.

    Erase [addr+1, addr+0x1001) should round to [addr, addr+0x2000),
    covering two full 4 KB sectors. Verify both sectors are 0xFF."""
    addr = base + 0x6000
    tool_load(addr, make_pattern(2 * SECTOR, seed=0x55), real=True)
    with Picotool() as pt:
        # Unaligned: starts 1 byte in, spans into second sector
        pt.erase(addr + 1, SECTOR)
    theirs = tool_save(addr, 2 * SECTOR, real=True)
    if not is_all_ff(theirs):
        n = sum(b != 0xFF for b in theirs)
        return '%d non-FF bytes (expected rounding to 2 full sectors)' % n
    return None


def test_verify_bytes_pass(base):
    """verify_bytes succeeds when flash matches expected data."""
    addr, size = base + 0x8000, SECTOR
    pattern = make_pattern(size, seed=0x66)
    with Picotool() as pt:
        pt.write(addr, pattern)
        try:
            pt.verify_bytes(addr, pattern)
        except PicotoolError as e:
            return 'verify_bytes raised on matching data: %s' % e
    return None


def test_verify_bytes_fail(base):
    """verify_bytes raises PicotoolError when flash doesn't match.

    Catches false-positive bugs where verify always passes."""
    addr, size = base + 0x9000, SECTOR
    pattern = make_pattern(size, seed=0x77)
    bad = bytearray(pattern)
    bad[100] ^= 0xFF
    with Picotool() as pt:
        pt.write(addr, pattern)
        try:
            pt.verify_bytes(addr, bytes(bad))
            return 'verify_bytes did not raise on mismatched data'
        except PicotoolError:
            pass  # expected
    return None


def test_guess_flash_size():
    """guess_flash_size returns a plausible power-of-2 value.

    Can't know the exact answer without hardware specs, but it must
    be > 0 (device has data), a power of 2, and within the range
    real picotool would report (256 KB to 32 MB)."""
    with Picotool() as pt:
        size = pt.guess_flash_size()
    if size == 0:
        return 'returned 0 (flash may be blank)'
    if size & (size - 1) != 0:
        return 'not a power of 2: %d' % size
    if size < 256 * 1024 or size > 32 * 1024 * 1024:
        return 'implausible size: %d' % size
    return None


# ---------------------------------------------------------------------------
#  Runner
# ---------------------------------------------------------------------------

def main():
    r = TestResults('test_flash_ops')

    if not has_bootsel_device():
        print('[test_flash_ops] SKIP (no BOOTSEL device)')
        r.record('all', SKIP, 'no BOOTSEL device')
        return r.summary()
    if not has_real_picotool():
        print('[test_flash_ops] SKIP (real picotool not in PATH)')
        r.record('all', SKIP, 'real picotool not in PATH')
        return r.summary()

    test_base, test_size = get_test_region()
    print('[test_flash_ops] (region 0x%08x, %d KB) ' % (test_base, test_size // 1024), end='')

    # FlashRegion runs outside any Picotool connection -- real picotool
    # handles backup/restore with no contention.
    with FlashRegion(test_base, test_size):
        r.run_test('read_cross_validate', lambda: test_read_cross_validate(test_base))
        r.run_test('write_read_roundtrip', lambda: test_write_read_roundtrip(test_base))
        r.run_test('write_cross_validate', lambda: test_write_cross_validate(test_base))
        r.run_test('real_write_our_read', lambda: test_real_write_our_read(test_base))
        r.run_test('erase_cross_validate', lambda: test_erase_cross_validate(test_base))
        r.run_test('erase_sector_alignment', lambda: test_erase_sector_alignment(test_base))
        r.run_test('verify_bytes_pass', lambda: test_verify_bytes_pass(test_base))
        r.run_test('verify_bytes_fail', lambda: test_verify_bytes_fail(test_base))
        r.run_test('guess_flash_size', test_guess_flash_size)

    return r.summary()


if __name__ == '__main__':
    sys.exit(main())
