"""
_harness.py -- Shared test infrastructure for picotool_py tests.

Provides device detection, cross-tool comparison, temp file management,
backup/restore, pattern generation, and result tracking. Test files
import this module and focus purely on test logic.

Design:
    - All subprocess invocations go through run_tool() which captures
      output and raises on failure with full context.
    - Flash region backup/restore is a context manager (FlashRegion)
      so cleanup happens even on exceptions.
    - Pattern generation is deterministic and seeded so different tests
      produce distinguishable data and failures are reproducible.
    - Result tracking is a simple list of (name, status, detail) tuples
      printed as a summary at the end.
"""

import os
import struct
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------------------
#  Path setup -- all test files live in tests/, project root is one up
# ---------------------------------------------------------------------------

TEST_DIR    = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TEST_DIR)

sys.path.insert(0, PROJECT_DIR)

OUR_CLI = os.path.join(PROJECT_DIR, 'picotool.py')


# ---------------------------------------------------------------------------
#  Subprocess helpers
# ---------------------------------------------------------------------------

def run_tool(cmd, check=True):
    """Run a command, return CompletedProcess. On failure (if check=True),
    raise RuntimeError with stdout+stderr for diagnosis."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        detail = (r.stdout.strip() + '\n' + r.stderr.strip()).strip()
        raise RuntimeError('command failed: %s\n  %s' % (
            ' '.join(cmd), detail))
    return r


def has_real_picotool():
    """True if the C++ picotool is in PATH."""
    try:
        run_tool(['picotool', 'version'])
        return True
    except (FileNotFoundError, RuntimeError):
        return False


def has_bootsel_device():
    """True if a Pico is currently in BOOTSEL mode."""
    try:
        from _wire import find_device, ConnectionError
        find_device()
        return True
    except Exception:
        return False


# Cached flash geometry, populated on first call to get_test_region().
_flash_size = None


def get_test_region(size=0x10000):
    """Return (addr, size) for the test region: the last `size` bytes
    of actual flash. Detects flash size on first call via our API.

    The test region is placed at the end of flash, far past any
    installed firmware, so it's safe to write/erase during tests."""
    global _flash_size
    if _flash_size is None:
        from picotool_lib import Picotool
        with Picotool() as pt:
            _flash_size = pt.guess_flash_size()
    if _flash_size == 0:
        raise RuntimeError('Cannot detect flash size (flash may be blank)')
    if size > _flash_size:
        raise RuntimeError('Test region %d > flash size %d' % (size, _flash_size))
    addr = 0x10000000 + _flash_size - size
    return addr, size


# ---------------------------------------------------------------------------
#  Cross-tool flash I/O -- the interchange layer between our tool and
#  the C++ reference. Every function takes a `real` flag: True = C++
#  picotool, False = our picotool.py.
# ---------------------------------------------------------------------------

def tool_save(addr, size, real=True):
    """Read flash range via CLI, return bytes."""
    tool = 'picotool' if real else OUR_CLI
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        path = f.name
    try:
        run_tool([tool, 'save', '--range',
                  '0x%x' % addr, '0x%x' % (addr + size), path])
        with open(path, 'rb') as f:
            return f.read()
    finally:
        os.unlink(path)


def tool_load(addr, data, real=True):
    """Write bytes to flash range via CLI."""
    tool = 'picotool' if real else OUR_CLI
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        f.write(data)
        path = f.name
    try:
        run_tool([tool, 'load', path, '--offset', '0x%x' % addr])
    finally:
        os.unlink(path)


def tool_erase(addr, size, real=True):
    """Erase flash range via CLI."""
    tool = 'picotool' if real else OUR_CLI
    run_tool([tool, 'erase', '--range',
              '0x%x' % addr, '0x%x' % (addr + size)])


# ---------------------------------------------------------------------------
#  Pattern generation -- deterministic, seeded, non-trivial.
#
#  The pattern must:
#    (a) not be all 0xFF (indistinguishable from erased flash)
#    (b) not be all 0x00 (indistinguishable from zero-fill)
#    (c) vary with the seed (so different tests produce different data)
#    (d) be reproducible (same seed + size = same bytes always)
# ---------------------------------------------------------------------------

def make_pattern(size, seed=0x5A):
    """Deterministic non-trivial byte pattern. The XOR/shift mix ensures
    no long runs of a single value."""
    return bytes([((i * 7 + seed) ^ (i >> 3)) & 0xFF for i in range(size)])


# ---------------------------------------------------------------------------
#  Byte comparison with first-diff reporting
# ---------------------------------------------------------------------------

def first_diff(a, b):
    """Return (index, a_byte, b_byte) of the first differing byte,
    or None if identical. Handles length mismatches."""
    min_len = min(len(a), len(b))
    for i in range(min_len):
        if a[i] != b[i]:
            return (i, a[i], b[i])
    if len(a) != len(b):
        return (min_len, None, None)  # length mismatch
    return None


def is_all_ff(data):
    """True if every byte is 0xFF (erased flash state)."""
    return len(data) > 0 and all(b == 0xFF for b in data)


# ---------------------------------------------------------------------------
#  Flash region backup/restore context manager
#
#  Usage:
#      with FlashRegion(0x10FF0000, 0x10000) as region:
#          # ... run tests that modify region.addr .. region.addr+region.size
#      # region is automatically restored to pre-test state
# ---------------------------------------------------------------------------

class FlashRegion:
    """Backs up a flash region before tests and restores it after,
    regardless of pass/fail. Uses real picotool for backup/restore
    to avoid trusting our own code for test infrastructure."""

    def __init__(self, addr, size):
        self.addr = addr
        self.size = size
        self._backup = None

    def __enter__(self):
        self._backup = tool_save(self.addr, self.size, real=True)
        return self

    def __exit__(self, exc_type, exc, tb):
        tool_erase(self.addr, self.size, real=True)
        if not is_all_ff(self._backup):
            tool_load(self.addr, self._backup, real=True)


# ---------------------------------------------------------------------------
#  Test result tracking
# ---------------------------------------------------------------------------

PASS = 'PASS'
FAIL = 'FAIL'
SKIP = 'SKIP'
ERROR = 'ERROR'


class TestResults:
    """Collects test results and prints a summary."""

    def __init__(self, suite_name):
        self.suite_name = suite_name
        self.results = []

    def record(self, name, status, detail=''):
        self.results.append((name, status, detail))
        mark = {'PASS': '.', 'FAIL': 'F', 'SKIP': 'S', 'ERROR': 'E'}
        sys.stdout.write(mark.get(status, '?'))
        sys.stdout.flush()

    def run_test(self, name, fn):
        """Run fn(), which returns None on pass or an error string on fail.
        Exceptions are caught and recorded as ERROR."""
        try:
            err = fn()
            if err is None:
                self.record(name, PASS)
            else:
                self.record(name, FAIL, err)
        except Exception as e:
            self.record(name, ERROR, str(e))

    def summary(self):
        """Print results and return exit code (0 = all pass/skip)."""
        print()
        passed = sum(1 for _, s, _ in self.results if s == PASS)
        failed = sum(1 for _, s, _ in self.results if s == FAIL)
        errors = sum(1 for _, s, _ in self.results if s == ERROR)
        skipped = sum(1 for _, s, _ in self.results if s == SKIP)

        # Print failures and errors with detail
        for name, status, detail in self.results:
            if status in (FAIL, ERROR):
                print('  %s %s: %s' % (status, name, detail))

        total = len(self.results)
        parts = []
        if passed:  parts.append('%d passed' % passed)
        if failed:  parts.append('%d failed' % failed)
        if errors:  parts.append('%d errors' % errors)
        if skipped: parts.append('%d skipped' % skipped)
        print('[%s] %d/%d  %s' % (
            self.suite_name, passed, total, ', '.join(parts)))
        return 0 if (failed + errors) == 0 else 1


# ---------------------------------------------------------------------------
#  Synthetic binary_info buffer construction
#
#  Builds a minimal flash buffer with the binary_info header and entries
#  that parse_binary_info() can scan. Used by test_binary_info.py to
#  test the parser against known inputs without needing real firmware.
#
#  Layout (matching pico-sdk binary_info/defs.h):
#    offset 0x00: MARKER_START
#    offset 0x04: pointer to bi_array_start
#    offset 0x08: pointer to bi_array_end
#    offset 0x0C: pointer to copy_table
#    offset 0x10: MARKER_END
#    ...
#    bi_array:    array of uint32_t pointers to entries
#    entries:     binary_info_t structs (core + payload)
#    strings:     NUL-terminated string data
#    copy_table:  terminated by a zero source_addr_start
# ---------------------------------------------------------------------------

# Constants re-declared here to avoid import-time dependency on _binary_info
# in test infrastructure. Values from pico-sdk structure.h.
_BI_MARKER_START = 0x7188EBF2
_BI_MARKER_END   = 0xE71AA390
_BI_TYPE_ID_AND_STRING = 6
_BI_TYPE_ID_AND_INT    = 5
_BI_TAG_RP = 0x5052


def build_binary_info_buffer(entries, base_addr=0x10000000, family='rp2350'):
    """Construct a synthetic flash buffer containing binary_info metadata.

    `entries` is a list of dicts:
        {'type': 'string', 'id': 0x02031c86, 'value': 'MyProgram'}
        {'type': 'int',    'id': 0x68f465de, 'value': 0x10040000}

    Returns bytes that parse_binary_info(buf, base_addr, family) can scan.
    All pointers are flash-absolute (no RAM remapping needed).
    """
    # Reserve space: header at offset 0, then entries, then strings.
    # We lay out linearly: header | padding | copy_table | bi_array | entries | strings
    header_offset = 0x100 if (family == 'rp2040' and base_addr == 0x10000000) else 0

    # Build entry blobs and string pool
    entry_blobs = []
    string_pool = bytearray()
    string_offsets = {}  # value -> offset in string_pool

    for e in entries:
        if e['type'] == 'string':
            val = e['value']
            if val not in string_offsets:
                string_offsets[val] = len(string_pool)
                string_pool.extend(val.encode('utf-8') + b'\x00')
            # core(4) + id(4) + value_ptr(4) = 12 bytes; ptr patched later
            entry_blobs.append(('string', e['id'], val))
        elif e['type'] == 'int':
            # core(4) + id(4) + value(4) = 12 bytes
            entry_blobs.append(('int', e['id'], e['value']))

    # Calculate layout offsets
    copy_table_off = header_offset + 20  # right after header
    copy_table_size = 4  # just the terminating zero word
    bi_array_off = copy_table_off + copy_table_size
    bi_array_size = len(entry_blobs) * 4
    entries_off = bi_array_off + bi_array_size
    # Each entry is 12 bytes
    strings_off = entries_off + len(entry_blobs) * 12
    total_size = strings_off + len(string_pool)

    # Ensure buffer is large enough for the scanner's read window.
    # RP2350 reads 256 bytes, RP2040 reads 64 bytes starting at 0x100.
    # Scanner reads max_words * 4 bytes: 256 words (1024 bytes) for RP2350,
    # 64 words (256 bytes) for RP2040 starting at header_offset.
    scan_bytes = 64 * 4 if family == 'rp2040' else 256 * 4
    min_size = header_offset + scan_bytes
    total_size = max(total_size, min_size)

    buf = bytearray(total_size)

    # Write header at header_offset
    struct.pack_into('<IIIII', buf, header_offset,
                     _BI_MARKER_START,
                     base_addr + bi_array_off,      # __binary_info_start
                     base_addr + bi_array_off + bi_array_size,  # __binary_info_end
                     base_addr + copy_table_off,     # __address_mapping_table
                     _BI_MARKER_END)

    # Write copy table (empty, just terminator)
    struct.pack_into('<I', buf, copy_table_off, 0)

    # Write entries and bi_array pointers
    for i, (etype, eid, eval_) in enumerate(entry_blobs):
        entry_addr = base_addr + entries_off + i * 12
        # bi_array pointer
        struct.pack_into('<I', buf, bi_array_off + i * 4, entry_addr)

        entry_off = entries_off + i * 12
        if etype == 'string':
            str_addr = base_addr + strings_off + string_offsets[eval_]
            struct.pack_into('<HH II', buf, entry_off,
                             _BI_TYPE_ID_AND_STRING, _BI_TAG_RP,
                             eid, str_addr)
        elif etype == 'int':
            struct.pack_into('<HH Ii', buf, entry_off,
                             _BI_TYPE_ID_AND_INT, _BI_TAG_RP,
                             eid, eval_)

    # Write string pool
    buf[strings_off:strings_off + len(string_pool)] = string_pool

    return bytes(buf)
