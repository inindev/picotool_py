"""
picotool_lib.py -- High-level Python API for the picotool replacement.

Wraps the low-level Connection from _wire.py with verbs that match the
picotool CLI: save, erase, load, reboot. Each method takes plain Python
arguments, returns Python values, and raises exceptions on error. The
CLI veneer (picotool.py) and library callers both go through this
class.

The library is intentionally silent: there's no print() anywhere in
this file. Callers that want a progress bar pass a `progress` callback
that the methods invoke periodically with (current, total) byte counts.
The CLI passes a ProgressBar.progress method.

Typical use:

    from picotool_lib import Picotool

    with Picotool() as pt:
        pt.save(0x10000000, 0x1000, 'firstpage.bin')
        pt.erase(0x100f0000, 0x1000)
        pt.load('app.bin', offset=0x10000000, verify=True)
        pt.reboot()

The context-manager form opens the device once and reuses the
connection across all calls, which is much faster than repeatedly
shelling out to picotool.
"""

import os

from _wire import (
    CommandFailure,
    ConnectionError,
    Connection,
    FLASH_END,
    FLASH_SECTOR_ERASE_SIZE,
    FLASH_START,
    PAGE_SIZE,
    REBOOT2_FLAG_REBOOT_TYPE_NORMAL,
    calculate_chunk_size,
    find_device,
)

__all__ = [
    'Picotool',
    'PicotoolError',
    'CommandFailure',
    'ConnectionError',
    'FLASH_START',
    'FLASH_END',
    'FLASH_SECTOR_ERASE_SIZE',
    'PAGE_SIZE',
]


class PicotoolError(Exception):
    """High-level picotool error: bad arguments, verify failed, file
    not found, etc. Distinct from CommandFailure (device-side status
    code) and ConnectionError (USB-level failure)."""


class Picotool:
    """High-level picotool API. Manages a single Connection internally.

    Use as a context manager whenever possible -- the connection is
    expensive to set up (USB enumerate + claim + reset) and reusing it
    across multiple operations is significantly faster than
    open-per-operation."""

    def __init__(self, serial=None):
        self.serial = serial
        self.dev = None
        self.family = None
        self.conn = None

    # -- Lifecycle --------------------------------------------------------

    def open(self):
        """Find the BOOTSEL device, claim PICOBOOT, reset, take
        exclusive access, and exit XIP. Idempotent."""
        if self.conn is not None:
            return
        self.dev, self.family = find_device(self.serial)
        self.conn = Connection(self.dev)
        self.conn.open_and_reset(exclusive=True)
        self.conn.exit_xip()

    def close(self):
        """Release exclusive access and dispose of the USB handle.
        Errors during close are swallowed because the device may be
        rebooting / disappearing."""
        if self.conn is None:
            return
        try:
            self.conn.close()
        except Exception:
            pass
        self.conn = None
        self.dev = None
        self.family = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # -- read / save ------------------------------------------------------

    def read(self, addr, size, progress=None):
        """Read `size` bytes of memory starting at `addr` and return
        them as a bytes object.

        progress(current, total): optional callback invoked
        periodically with byte counts during the read loop.

        Returns the bytes read. Raises PicotoolError on argument
        errors.

        Bytes-oriented counterpart to save(). Mirrors the read loop
        in save_command::execute, main.cpp:4602-4615.
        """
        if size <= 0:
            raise PicotoolError('Read range is invalid/empty')

        self.open()

        chunk_size = calculate_chunk_size(size)
        end = addr + size
        out = bytearray()
        base = addr
        while base < end:
            if progress is not None:
                progress(base - addr, size)
            this_chunk = min(chunk_size, end - base)
            out.extend(self.conn.read_memory(base, this_chunk))
            base += this_chunk
        if progress is not None:
            progress(size, size)
        return bytes(out)

    def save(self, addr, size, file_path, progress=None):
        """Read `size` bytes of memory starting at `addr` and write
        them to `file_path` as a flat binary.

        Returns the number of bytes written. Raises PicotoolError on
        argument errors.

        Mirrors save_command::execute (BIN-only path), main.cpp:4495-4625.
        """
        data = self.read(addr, size, progress=progress)
        with open(file_path, 'wb') as out:
            out.write(data)
        return len(data)

    # -- erase ------------------------------------------------------------

    def erase(self, addr, size, progress=None):
        """Erase flash sectors covering [addr, addr+size).

        Bounds are auto-rounded outward to FLASH_SECTOR_ERASE_SIZE,
        matching real picotool's behavior. After this call all bytes
        in the rounded range read back as 0xFF.

        Returns the actual byte count erased (after rounding). Raises
        PicotoolError on argument errors.

        Mirrors erase_command::execute (range path), main.cpp:4673-4738.
        """
        # main.cpp:4709-4710 -- expand outward
        start = addr & ~(FLASH_SECTOR_ERASE_SIZE - 1)
        end = (addr + size + FLASH_SECTOR_ERASE_SIZE - 1) & \
              ~(FLASH_SECTOR_ERASE_SIZE - 1)
        if end <= start:
            raise PicotoolError('Erase range is invalid/empty')
        # main.cpp:4721-4726 -- both ends in flash
        if not (FLASH_START <= start < FLASH_END) or \
           not (FLASH_START <= end <= FLASH_END):
            raise PicotoolError('Erase range not all in flash')

        self.open()

        total = end - start
        cur = start
        while cur < end:
            if progress is not None:
                progress(cur - start, total)
            self.conn.flash_erase(cur, FLASH_SECTOR_ERASE_SIZE)
            cur += FLASH_SECTOR_ERASE_SIZE
        if progress is not None:
            progress(total, total)
        return total

    # -- write / load / verify --------------------------------------------
    #
    # `write` (bytes) is the foundation; `load` (file) is a thin wrapper.
    # Same shape for verify_bytes / verify. The bytes-oriented forms are
    # what library callers usually want; the file-oriented forms exist
    # for CLI parity with picotool.

    def _read_bin_file(self, file_path):
        """Helper: read a BIN file, validate it's non-empty."""
        if not os.path.exists(file_path):
            raise PicotoolError('file not found: %s' % file_path)
        with open(file_path, 'rb') as f:
            file_data = f.read()
        if len(file_data) == 0:
            raise PicotoolError('file is empty')
        return file_data

    def _check_flash_range(self, range_from, range_to):
        if not (FLASH_START <= range_from < FLASH_END) or \
           not (FLASH_START <= range_to <= FLASH_END):
            raise PicotoolError(
                'range 0x%08x-0x%08x not all in flash' %
                (range_from, range_to))

    def write(self, addr, data, progress=None):
        """Write `data` bytes to flash starting at `addr`.

        Erases the surrounding sectors as needed (with zero-fill for
        any front/back partial sector), then writes the data.

        progress(current, total): write-phase callback.

        Returns the number of bytes written. Raises PicotoolError on
        argument errors.

        Bytes-oriented counterpart to load(). Mirrors the write path
        of load_guts, main.cpp:4845-4888.
        """
        if not data:
            raise PicotoolError('Write data is empty')

        range_from = addr
        range_to = addr + len(data)
        self._check_flash_range(range_from, range_to)

        self.open()

        batch_size = calculate_chunk_size(range_to - range_from)
        base = range_from
        while base < range_to:
            this_batch = min(range_to - base, batch_size)
            # main.cpp:4859-4860 -- expand outward to sectors
            aligned_from = base & ~(FLASH_SECTOR_ERASE_SIZE - 1)
            aligned_to = ((base + this_batch + FLASH_SECTOR_ERASE_SIZE - 1)
                          & ~(FLASH_SECTOR_ERASE_SIZE - 1))
            aligned_len = aligned_to - aligned_from
            # main.cpp:4861-4862 -- read range = batch intersect aligned
            read_from = max(base, aligned_from)
            read_to = min(base + this_batch, aligned_to)
            data_off = read_from - range_from
            chunk = data[data_off:data_off + (read_to - read_from)]
            # main.cpp:4865-4866 -- zero pad to aligned bounds
            pre_pad = read_from - aligned_from
            post_pad = aligned_to - read_to
            buf = (b'\x00' * pre_pad) + chunk + (b'\x00' * post_pad)
            assert len(buf) == aligned_len, \
                'pad mismatch: %d vs %d' % (len(buf), aligned_len)

            # main.cpp:4876-4878 -- erase + write
            self.conn.flash_erase(aligned_from, aligned_len)
            self.conn.write(aligned_from, buf)

            base = read_to
            if progress is not None:
                progress(base - range_from, range_to - range_from)
        if progress is not None:
            progress(range_to - range_from, range_to - range_from)
        return len(data)

    def load(self, file_path, offset=FLASH_START, progress=None):
        """Load `file_path` (a BIN file) into flash starting at `offset`.

        Returns the number of bytes loaded. Raises PicotoolError on
        argument errors.

        Mirrors the write path of load_guts, main.cpp:4845-4888.
        """
        file_data = self._read_bin_file(file_path)
        return self.write(offset, file_data, progress=progress)

    def verify_bytes(self, addr, expected, progress=None):
        """Verify that flash starting at `addr` matches `expected` bytes.

        Reads the device range and byte-compares against `expected`.

        progress(current, total): callback for the read loop.

        Raises PicotoolError on first mismatch (with offset and bytes
        in the message). Returns the number of bytes verified on success.

        Bytes-oriented counterpart to verify(). Mirrors the verify pass
        of load_guts, main.cpp:4892-4928.
        """
        if not expected:
            raise PicotoolError('Verify data is empty')

        range_from = addr
        range_to = addr + len(expected)
        self._check_flash_range(range_from, range_to)

        self.open()

        batch_size = calculate_chunk_size(range_to - range_from)
        base = range_from
        while base < range_to:
            this_batch = min(range_to - base, batch_size)
            data_off = base - range_from
            chunk = expected[data_off:data_off + this_batch]
            device_buf = self.conn.read_memory(base, this_batch)
            for i in range(this_batch):
                if chunk[i] != device_buf[i]:
                    raise PicotoolError(
                        'verify failed at 0x%x: expected 0x%02x device 0x%02x'
                        % (base + i, chunk[i], device_buf[i]))
            base += this_batch
            if progress is not None:
                progress(base - range_from, range_to - range_from)
        if progress is not None:
            progress(range_to - range_from, range_to - range_from)
        return len(expected)

    def verify(self, file_path, offset=FLASH_START, progress=None):
        """Verify that flash starting at `offset` matches `file_path`.

        Mirrors the verify pass of load_guts, main.cpp:4892-4928.
        """
        file_data = self._read_bin_file(file_path)
        return self.verify_bytes(offset, file_data, progress=progress)

    # -- reboot -----------------------------------------------------------

    def reboot(self):
        """Reboot the device into application mode.

        On RP2350 uses PC_REBOOT2 with REBOOT_TYPE_NORMAL (the legacy
        PC_REBOOT does not actually leave BOOTSEL on RP2350). On RP2040
        uses the legacy PC_REBOOT.

        Mirrors reboot_command::execute (default app-mode path),
        main.cpp:8519-8593.
        """
        self.open()
        if self.family == 'rp2350':
            self.conn.reboot2(
                flags=REBOOT2_FLAG_REBOOT_TYPE_NORMAL,
                delay_ms=500,
                param0=0,
                param1=0,
            )
        else:
            self.conn.reboot(0, 0, 500)
