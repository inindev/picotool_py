#!/usr/bin/env python3
"""
test_cli.py -- Integration tests for the picotool.py CLI.

Validates that argparse wiring, exit codes, and output formatting
work correctly. Uses subprocess to invoke picotool.py just as a
user would.

Hardware tests are self-contained: no Picotool connection is held
during subprocess calls, so real picotool and our CLI can each
claim the device as needed.

Offline tests (no device): help output, file info, error handling.
Hardware tests (device + real picotool): save/load/erase cross-validated.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from _harness import (
    TestResults, FlashRegion, first_diff, get_test_region,
    has_bootsel_device, has_real_picotool, is_all_ff, make_pattern,
    run_tool, tool_save, tool_load, build_binary_info_buffer, OUR_CLI,
    SKIP,
)
from _binary_info import BINARY_INFO_ID_RP_PROGRAM_NAME

SECTOR = 0x1000


# ---------------------------------------------------------------------------
#  Offline tests (no device needed)
# ---------------------------------------------------------------------------

def test_help_exits_zero():
    """picotool.py --help exits 0."""
    r = run_tool([OUR_CLI, '--help'], check=False)
    if r.returncode != 0:
        return 'exit code %d' % r.returncode
    if 'usage:' not in r.stdout.lower():
        return '"usage:" not in help output'
    return None


def test_subcommand_help():
    """Each subcommand --help exits 0."""
    for cmd in ('info', 'save', 'erase', 'load', 'verify', 'reboot'):
        r = run_tool([OUR_CLI, cmd, '--help'], check=False)
        if r.returncode != 0:
            return '%s --help exit code %d' % (cmd, r.returncode)
    return None


def test_info_file_cli():
    """picotool.py info <file.bin> prints program name, exits 0."""
    buf = build_binary_info_buffer([
        {'type': 'string', 'id': BINARY_INFO_ID_RP_PROGRAM_NAME,
         'value': 'cli_test_app'},
    ])
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        f.write(buf)
        path = f.name
    try:
        r = run_tool([OUR_CLI, 'info', path], check=False)
        if r.returncode != 0:
            return 'exit code %d: %s' % (r.returncode, r.stderr.strip())
        if 'cli_test_app' not in r.stdout:
            return '"cli_test_app" not in output: %s' % r.stdout.strip()
    finally:
        os.unlink(path)
    return None


def test_load_empty_file_errors():
    """picotool.py load <empty.bin> exits non-zero with error message."""
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        path = f.name
    try:
        r = run_tool([OUR_CLI, 'load', path], check=False)
        if r.returncode == 0:
            return 'should have failed for empty file'
    finally:
        os.unlink(path)
    return None


def test_load_missing_file_errors():
    """picotool.py load <nonexistent> exits non-zero."""
    r = run_tool([OUR_CLI, 'load', '/nonexistent/path.bin'], check=False)
    if r.returncode == 0:
        return 'should have failed for missing file'
    return None


# ---------------------------------------------------------------------------
#  Hardware tests (device + real picotool)
# ---------------------------------------------------------------------------

def test_cli_save_range_cross_validate(base):
    """CLI save --range produces file matching real picotool readback."""
    addr, size = base, SECTOR
    ref = tool_save(addr, size, real=True)

    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        path = f.name
    try:
        # Our CLI runs as subprocess -- no connection contention
        run_tool([OUR_CLI, 'save', '--range',
                  '0x%x' % addr, '0x%x' % (addr + size), path])
        with open(path, 'rb') as f:
            ours = f.read()
        d = first_diff(ours, ref)
        if d is not None:
            return 'mismatch at offset 0x%x' % d[0]
    finally:
        os.unlink(path)
    return None


def test_cli_load_verify(base):
    """CLI load -v exits 0 and prints OK for matching data."""
    addr, size = base + 0x1000, SECTOR
    pattern = make_pattern(size, seed=0xC1)
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        f.write(pattern)
        path = f.name
    try:
        r = run_tool([OUR_CLI, 'load', path, '-o', '0x%x' % addr, '-v'],
                     check=False)
        if r.returncode != 0:
            return 'exit code %d: %s' % (r.returncode, r.stderr.strip())
        if 'OK' not in r.stdout:
            return '"OK" not in output: %s' % r.stdout.strip()
    finally:
        os.unlink(path)
    return None


def test_cli_erase_range(base):
    """CLI erase --range followed by real picotool readback: all 0xFF."""
    addr, size = base + 0x2000, SECTOR
    tool_load(addr, make_pattern(size, seed=0xC2), real=True)
    # Our CLI erases as subprocess
    run_tool([OUR_CLI, 'erase', '--range',
              '0x%x' % addr, '0x%x' % (addr + size)])
    # Real picotool reads back
    ref = tool_save(addr, size, real=True)
    if not is_all_ff(ref):
        n = sum(b != 0xFF for b in ref)
        return '%d non-FF bytes after CLI erase' % n
    return None


# ---------------------------------------------------------------------------
#  Runner
# ---------------------------------------------------------------------------

def main():
    r = TestResults('test_cli')
    print('[test_cli] ', end='')

    # Offline tests always run
    r.run_test('help_exits_zero', test_help_exits_zero)
    r.run_test('subcommand_help', test_subcommand_help)
    r.run_test('info_file_cli', test_info_file_cli)
    r.run_test('load_empty_file_errors', test_load_empty_file_errors)
    r.run_test('load_missing_file_errors', test_load_missing_file_errors)

    # Hardware tests
    if has_bootsel_device() and has_real_picotool():
        test_base, test_size = get_test_region()
        with FlashRegion(test_base, test_size):
            r.run_test('cli_save_range_cross_validate',
                       lambda: test_cli_save_range_cross_validate(test_base))
            r.run_test('cli_load_verify',
                       lambda: test_cli_load_verify(test_base))
            r.run_test('cli_erase_range',
                       lambda: test_cli_erase_range(test_base))
    else:
        for name in ('cli_save_range_cross_validate',
                     'cli_load_verify', 'cli_erase_range'):
            r.record(name, SKIP, 'no device or picotool')

    return r.summary()


if __name__ == '__main__':
    sys.exit(main())
