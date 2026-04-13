#!/usr/bin/env python3
"""
test_uf2.py -- Offline tests for the _uf2 module.

Tests parse_uf2() and create_uf2() against known buffer contents,
not round-trip. This catches offset/alignment bugs that a symmetric
parse(create(x)) == x round-trip would mask if both sides share
the same bug.

No hardware required. No C++ picotool required.
"""

import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from _harness import TestResults
from _uf2 import (
    UF2Error,
    UF2_BLOCK_SIZE,
    UF2_FLAG_FAMILY_ID_PRESENT,
    UF2_FLAG_NOT_MAIN_FLASH,
    UF2_MAGIC_END,
    UF2_MAGIC_START0,
    UF2_MAGIC_START1,
    UF2_PAGE_SIZE,
    ABSOLUTE_FAMILY_ID,
    RP2040_FAMILY_ID,
    RP2350_ARM_S_FAMILY_ID,
    RP2350_RISCV_FAMILY_ID,
    _is_abs_block,
    create_uf2,
    parse_uf2,
)


def _write_tmp(data):
    """Write bytes to a temp .uf2 file, return path."""
    f = tempfile.NamedTemporaryFile(suffix='.uf2', delete=False)
    f.write(data)
    f.close()
    return f.name


def _make_block(target_addr, payload, block_no, num_blocks, family_id,
                flags=UF2_FLAG_FAMILY_ID_PRESENT):
    """Construct a single 512-byte UF2 block from explicit fields.
    This is independent of create_uf2() -- it builds from the spec
    so we can test the parser against a known-correct source."""
    blk = bytearray(UF2_BLOCK_SIZE)
    struct.pack_into('<IIIIIIII', blk, 0,
                     UF2_MAGIC_START0, UF2_MAGIC_START1,
                     flags, target_addr, len(payload),
                     block_no, num_blocks, family_id)
    blk[32:32 + len(payload)] = payload
    struct.pack_into('<I', blk, 508, UF2_MAGIC_END)
    return bytes(blk)


# ---------------------------------------------------------------------------
#  Tests
# ---------------------------------------------------------------------------

def test_parse_known_block():
    """Parser extracts correct addr/data from a hand-built block."""
    payload = bytes(range(256))
    blk = _make_block(0x10000000, payload, 0, 1, RP2350_ARM_S_FAMILY_ID)
    path = _write_tmp(blk)
    try:
        fam, img, start, end = parse_uf2(path)
        if fam != RP2350_ARM_S_FAMILY_ID:
            return 'family: expected 0x%08x got 0x%08x' % (
                RP2350_ARM_S_FAMILY_ID, fam)
        if start != 0x10000000:
            return 'start: expected 0x10000000 got 0x%08x' % start
        if end != 0x10000100:
            return 'end: expected 0x10000100 got 0x%08x' % end
        if bytes(img) != payload:
            return 'payload mismatch'
    finally:
        os.unlink(path)
    return None


def test_create_block_structure():
    """create_uf2() produces blocks matching the UF2 spec byte-for-byte.
    We check field positions against the struct definition in uf2.h."""
    data = bytes(range(256))
    uf2 = create_uf2(data, 0x10020000, RP2040_FAMILY_ID)
    if len(uf2) != UF2_BLOCK_SIZE:
        return 'expected %d bytes, got %d' % (UF2_BLOCK_SIZE, len(uf2))
    # Verify each field at its defined offset
    ms0, ms1, flags, taddr, psize, bno, nblks, fam = \
        struct.unpack_from('<IIIIIIII', uf2, 0)
    mend, = struct.unpack_from('<I', uf2, 508)
    if ms0 != UF2_MAGIC_START0: return 'magic0 wrong'
    if ms1 != UF2_MAGIC_START1: return 'magic1 wrong'
    if flags != UF2_FLAG_FAMILY_ID_PRESENT: return 'flags wrong'
    if taddr != 0x10020000: return 'target_addr wrong'
    if psize != 256: return 'payload_size wrong'
    if bno != 0: return 'block_no wrong'
    if nblks != 1: return 'num_blocks wrong'
    if fam != RP2040_FAMILY_ID: return 'family wrong'
    if mend != UF2_MAGIC_END: return 'magic_end wrong'
    if uf2[32:32 + 256] != data: return 'data payload wrong'
    return None


def test_multi_page_round_trip():
    """Multiple pages: create then parse, verify data integrity.
    This IS a round-trip test, but complements test_create_block_structure
    which verifies the format independently."""
    data = bytes(range(256)) * 8  # 2048 bytes = 8 pages
    uf2 = create_uf2(data, 0x10000000, RP2350_ARM_S_FAMILY_ID)
    if len(uf2) != 8 * UF2_BLOCK_SIZE:
        return 'expected %d bytes, got %d' % (8 * UF2_BLOCK_SIZE, len(uf2))
    path = _write_tmp(uf2)
    try:
        fam, img, start, end = parse_uf2(path)
        if bytes(img) != data:
            return 'round-trip data mismatch'
        if start != 0x10000000 or end != 0x10000800:
            return 'address range wrong'
    finally:
        os.unlink(path)
    return None


def test_short_final_page():
    """Data not a multiple of 256: last page is zero-padded in UF2,
    but parse_uf2 should return the original unpadded range."""
    data = bytes([0xAA] * 300)  # 1 full page + 44 bytes
    uf2 = create_uf2(data, 0x10000000, RP2350_ARM_S_FAMILY_ID)
    if len(uf2) != 2 * UF2_BLOCK_SIZE:
        return 'expected 2 blocks, got %d' % (len(uf2) // UF2_BLOCK_SIZE)
    path = _write_tmp(uf2)
    try:
        fam, img, start, end = parse_uf2(path)
        # end should be 0x10000100 + 44 = 0x1000012C (second block's
        # target_addr + payload_size covers the padded page)
        # Actually: block 1 target=0x10000100, payload=256, so end=0x10000200
        # The image will be 512 bytes with padding. The original 300 bytes
        # are at img[0:300], and img[300:512] is zero-padded.
        if end != 0x10000200:
            return 'end: expected 0x10000200 got 0x%08x' % end
        if bytes(img[:300]) != data:
            return 'first 300 bytes mismatch'
    finally:
        os.unlink(path)
    return None


def test_family_filter():
    """parse_uf2 with restricted valid_families skips non-matching blocks."""
    blk = _make_block(0x10000000, bytes(256), 0, 1, RP2350_RISCV_FAMILY_ID)
    path = _write_tmp(blk)
    try:
        # Should succeed with matching family
        fam, _, _, _ = parse_uf2(path, valid_families={RP2350_RISCV_FAMILY_ID})
        if fam != RP2350_RISCV_FAMILY_ID:
            return 'expected RISCV family'
        # Should fail with non-matching family (all blocks skipped)
        try:
            parse_uf2(path, valid_families={RP2040_FAMILY_ID})
            return 'should have raised UF2Error for wrong family'
        except UF2Error:
            pass  # expected
    finally:
        os.unlink(path)
    return None


def test_not_main_flash_skipped():
    """Blocks with UF2_FLAG_NOT_MAIN_FLASH are skipped by the parser."""
    blk = _make_block(0x10000000, bytes(256), 0, 1, RP2350_ARM_S_FAMILY_ID,
                      flags=UF2_FLAG_FAMILY_ID_PRESENT | UF2_FLAG_NOT_MAIN_FLASH)
    path = _write_tmp(blk)
    try:
        try:
            parse_uf2(path)
            return 'should have raised UF2Error (no valid blocks)'
        except UF2Error:
            pass
    finally:
        os.unlink(path)
    return None


def test_abs_block_detection():
    """RP2350-E10 absolute block is correctly identified and skipped."""
    # Build an absolute block per main.cpp:2936-2966 spec
    blk = bytearray(UF2_BLOCK_SIZE)
    struct.pack_into('<IIIIIIII', blk, 0,
                     UF2_MAGIC_START0, UF2_MAGIC_START1,
                     UF2_FLAG_FAMILY_ID_PRESENT,
                     0x10FFFF00,   # target_addr
                     UF2_PAGE_SIZE,
                     0,            # block_no
                     2,            # num_blocks (signature: must be 2)
                     ABSOLUTE_FAMILY_ID)
    blk[32:32 + UF2_PAGE_SIZE] = bytes([0xEF] * UF2_PAGE_SIZE)
    struct.pack_into('<I', blk, 508, UF2_MAGIC_END)

    if not _is_abs_block(bytes(blk)):
        return 'abs block not detected'

    # Modify one field and verify it's no longer detected
    blk_bad = bytearray(blk)
    blk_bad[32] = 0x00  # break the 0xEF fill
    if _is_abs_block(bytes(blk_bad)):
        return 'corrupted abs block should not be detected'
    return None


def test_bad_magic_rejected():
    """File with corrupted magic raises UF2Error."""
    blk = bytearray(_make_block(0x10000000, bytes(256), 0, 1,
                                RP2350_ARM_S_FAMILY_ID))
    blk[0:4] = b'\x00\x00\x00\x00'  # corrupt magic_start0
    path = _write_tmp(bytes(blk))
    try:
        try:
            parse_uf2(path)
            return 'should have raised UF2Error for bad magic'
        except UF2Error:
            pass
    finally:
        os.unlink(path)
    return None


def test_empty_file_rejected():
    """Empty file raises UF2Error."""
    path = _write_tmp(b'')
    try:
        try:
            parse_uf2(path)
            return 'should have raised UF2Error for empty file'
        except UF2Error:
            pass
    finally:
        os.unlink(path)
    return None


def test_truncated_file_rejected():
    """File not a multiple of 512 raises UF2Error."""
    path = _write_tmp(b'\x00' * 100)
    try:
        try:
            parse_uf2(path)
            return 'should have raised UF2Error for truncated file'
        except UF2Error:
            pass
    finally:
        os.unlink(path)
    return None


# ---------------------------------------------------------------------------
#  Runner
# ---------------------------------------------------------------------------

def main():
    r = TestResults('test_uf2')
    print('[test_uf2] ', end='')

    r.run_test('parse_known_block', test_parse_known_block)
    r.run_test('create_block_structure', test_create_block_structure)
    r.run_test('multi_page_round_trip', test_multi_page_round_trip)
    r.run_test('short_final_page', test_short_final_page)
    r.run_test('family_filter', test_family_filter)
    r.run_test('not_main_flash_skipped', test_not_main_flash_skipped)
    r.run_test('abs_block_detection', test_abs_block_detection)
    r.run_test('bad_magic_rejected', test_bad_magic_rejected)
    r.run_test('empty_file_rejected', test_empty_file_rejected)
    r.run_test('truncated_file_rejected', test_truncated_file_rejected)

    return r.summary()


if __name__ == '__main__':
    sys.exit(main())
