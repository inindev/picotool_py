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
    REBOOT2_FLAG_REBOOT_TO_ARM,
    REBOOT2_FLAG_REBOOT_TO_RISCV,
    REBOOT2_FLAG_REBOOT_TYPE_BOOTSEL,
    REBOOT2_FLAG_REBOOT_TYPE_NORMAL,
    calculate_chunk_size,
    find_device,
)
from _binary_info import parse_binary_info
from _uf2 import (
    UF2Error,
    parse_uf2,
    create_uf2,
    FAMILY_NAMES,
    RP2040_FAMILY_ID,
    RP2350_ARM_S_FAMILY_ID,
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


def _file_type_from_ext(path):
    """Infer file type from extension: 'uf2' or 'bin'."""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.uf2':
        return 'uf2'
    return 'bin'


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

    def save(self, addr, size, file_path, file_type=None, family_id=None,
             progress=None):
        """Read `size` bytes of memory starting at `addr` and write
        them to `file_path` as BIN or UF2.

        `file_type` -- 'bin' or 'uf2'; auto-detected from extension
                       if None. Mirrors main.cpp:4576-4594 (UF2 writer)
                       and main.cpp:4556-4571 (BIN writer).
        `family_id` -- UF2 family ID; auto-detected from device if None.

        Returns the number of bytes written. Raises PicotoolError on
        argument errors.

        Mirrors save_command::execute (--range path), main.cpp:4495-4625.
        """
        if file_type is None:
            file_type = _file_type_from_ext(file_path)
        return self._save_range(addr, size, file_path, file_type,
                                family_id, progress)

    # -- flash size detection -----------------------------------------------

    def guess_flash_size(self):
        """Detect actual flash size by checking for address mirroring.

        Returns the flash size in bytes, or 0 if flash appears erased.

        Mirrors main.cpp:2840-2860 (guess_flash_size). The algorithm
        reads the first two 256-byte pages at FLASH_START, then tests
        successively smaller power-of-2 offsets (8 MB down to 4 KB).
        Flash chips mirror their contents at power-of-2 boundaries,
        so when the data at FLASH_START + offset differs from the
        data at FLASH_START, the real flash is at least offset*2.
        """
        self.open()

        # main.cpp:2843 -- read first two pages
        first_two = self.conn.read_memory(FLASH_START, 2 * PAGE_SIZE)
        page0 = first_two[:PAGE_SIZE]
        page1 = first_two[PAGE_SIZE:]

        # main.cpp:2844-2848 -- if both pages are identical, flash is erased
        if page0 == page1:
            return 0

        # main.cpp:2852-2858 -- binary search on mirroring
        min_size = 16 * PAGE_SIZE      # 4096 = 0x1000
        max_size = 8 * 1024 * 1024     # 8 MB = 0x800000
        size = max_size
        while size >= min_size:
            new_pages = self.conn.read_memory(FLASH_START + size, 2 * PAGE_SIZE)
            if new_pages != first_two:
                break
            size >>= 1

        # main.cpp:2859
        return size * 2

    # -- info ---------------------------------------------------------------

    def info(self, progress=None):
        """Read binary_info metadata from the device and return a
        BinaryInfo object (or None if no metadata found).

        Reads the first portion of flash to locate the binary_info
        header, then parses all entries.

        Mirrors the binary_info scanning in main.cpp:3546-3599
        (info_guts) and main.cpp:2456-2495 (find_binary_info).
        """
        self.open()

        # Read enough flash to cover the binary_info header and its
        # pointer array. On RP2040 the header is within the first
        # 0x100+64 bytes; on RP2350 within the first 256. The pointer
        # array and strings it references can be anywhere in the binary,
        # so we read a generous chunk. 256 KB covers the typical SDK
        # binary's .rodata where strings live.
        read_size = 256 * 1024
        if progress:
            progress(0, read_size)
        buf = self.read(FLASH_START, read_size, progress=progress)
        return parse_binary_info(buf, FLASH_START, self.family)

    def info_file(self, file_path):
        """Parse binary_info metadata from a file (BIN or UF2) without
        a device connection.

        Returns a BinaryInfo object, or None if no metadata found.

        Mirrors info_command::execute when targeting a file instead of
        a device (main.cpp:3227-3233, 3546-3599).
        """
        file_type = _file_type_from_ext(file_path)

        if file_type == 'uf2':
            try:
                family_id, image, addr_min, addr_max = parse_uf2(file_path)
            except UF2Error as e:
                raise PicotoolError('UF2 parse error: %s' % e)
            # Determine family for scan window (RP2040 vs RP2350)
            from _uf2 import RP2040_FAMILY_ID as _RP2040
            family = 'rp2040' if family_id == _RP2040 else 'rp2350'
            return parse_binary_info(bytes(image), addr_min, family)
        else:
            if not os.path.exists(file_path):
                raise PicotoolError('file not found: %s' % file_path)
            with open(file_path, 'rb') as f:
                data = f.read()
            if not data:
                raise PicotoolError('file is empty')
            # BIN files loaded at FLASH_START by convention
            return parse_binary_info(data, FLASH_START, 'rp2350')

    # -- save extended modes ------------------------------------------------

    def save_program(self, file_path, file_type=None, family_id=None,
                     progress=None):
        """Save the program from flash to a file.

        Determines the program extent from binary_info
        (BINARY_INFO_ID_RP_BINARY_END). Equivalent to `picotool save`
        with no --range or --all flag (the default --program mode).

        `file_type` -- 'bin' or 'uf2'; auto-detected from extension
                       if None.
        `family_id` -- UF2 family ID; auto-detected from device family
                       if None. Only used for UF2 output.

        Returns the number of bytes written.

        Mirrors save_command::execute (program path), main.cpp:4518-4541.
        """
        self.open()

        # Determine file type from extension if not specified
        if file_type is None:
            file_type = _file_type_from_ext(file_path)

        # main.cpp:4519-4528 -- read binary_info to find binary_end
        info = self.info()
        end = 0
        if info is not None and info.binary_end:
            end = info.binary_end

        # main.cpp:4537-4540
        if end == 0:
            raise PicotoolError(
                'Cannot determine binary size from binary_info. '
                'Try save --all or save --range instead.')

        start = FLASH_START
        size = end - start

        return self._save_range(start, size, file_path, file_type,
                                family_id, progress)

    def save_all(self, file_path, file_type=None, family_id=None,
                 progress=None):
        """Save the entire flash contents to a file.

        Uses guess_flash_size() to detect the actual flash size,
        then saves from FLASH_START to FLASH_START + flash_size.

        Mirrors save_command::execute (--all path), main.cpp:4542-4547.
        """
        self.open()

        if file_type is None:
            file_type = _file_type_from_ext(file_path)

        # main.cpp:4543
        flash_size = self.guess_flash_size()
        if flash_size == 0:
            raise PicotoolError(
                'Cannot determine flash size (flash may be erased). '
                'Try save --range instead.')

        return self._save_range(FLASH_START, flash_size, file_path,
                                file_type, family_id, progress)

    def _save_range(self, addr, size, file_path, file_type, family_id,
                    progress):
        """Internal: read a range and write to file as BIN or UF2.

        For UF2 output, aligns to PAGE_SIZE boundaries as picotool does
        (main.cpp:4506-4507). For BIN, uses exact addresses.
        """
        start = addr
        end = addr + size

        # main.cpp:4505-4507 -- UF2 requires PAGE_SIZE alignment
        if file_type == 'uf2':
            start = start & ~(PAGE_SIZE - 1)
            end = (end + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1)
            size = end - start

        data = self.read(start, size, progress=progress)

        if file_type == 'uf2':
            # main.cpp:4582 -- auto-detect family ID from device
            if family_id is None:
                if self.family == 'rp2040':
                    family_id = RP2040_FAMILY_ID
                else:
                    family_id = RP2350_ARM_S_FAMILY_ID
            uf2_data = create_uf2(data, start, family_id)
            with open(file_path, 'wb') as f:
                f.write(uf2_data)
            return len(uf2_data)
        else:
            with open(file_path, 'wb') as f:
                f.write(data)
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

    def erase_all(self, progress=None):
        """Erase the entire flash.

        Uses guess_flash_size() to detect the actual flash size, then
        erases from FLASH_START to FLASH_START + flash_size.

        Returns the number of bytes erased.

        Mirrors erase_command::execute (default --all path),
        main.cpp:4714-4719.
        """
        self.open()

        # main.cpp:4715
        flash_size = self.guess_flash_size()
        if flash_size == 0:
            raise PicotoolError(
                'Cannot determine flash size (flash may be erased). '
                'Try erase --range instead.')

        return self.erase(FLASH_START, flash_size, progress=progress)

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

    def load(self, file_path, offset=FLASH_START, file_type=None,
             family_id=None, progress=None):
        """Load a BIN or UF2 file into flash.

        For BIN files, writes starting at `offset` (default FLASH_START).
        For UF2 files, uses the target addresses embedded in the UF2
        blocks; the `offset` parameter is ignored.

        `file_type` -- 'bin' or 'uf2'; auto-detected from extension
                       if None. Mirrors C++ -t/--type flag (main.cpp:703).
        `family_id` -- for UF2 files, restrict to this family ID.
                       Mirrors C++ --family flag (main.cpp:849-850).

        Returns the number of bytes loaded. Raises PicotoolError on
        argument errors.

        Mirrors the write path of load_guts, main.cpp:4845-4888.
        """
        if file_type is None:
            file_type = _file_type_from_ext(file_path)

        if file_type == 'uf2':
            try:
                valid = None
                if family_id is not None:
                    valid = {family_id}
                fam, image, addr_min, addr_max = parse_uf2(
                    file_path, valid_families=valid)
            except UF2Error as e:
                raise PicotoolError('UF2 parse error: %s' % e)
            return self.write(addr_min, bytes(image), progress=progress)
        else:
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

    def verify(self, file_path, offset=FLASH_START, file_type=None,
               progress=None):
        """Verify that flash matches `file_path`.

        For BIN files, verifies starting at `offset`.
        For UF2 files, uses the target addresses from the UF2 blocks.

        `file_type` -- 'bin' or 'uf2'; auto-detected from extension
                       if None.

        Mirrors the verify pass of load_guts, main.cpp:4892-4928.
        """
        if file_type is None:
            file_type = _file_type_from_ext(file_path)

        if file_type == 'uf2':
            try:
                family_id, image, addr_min, addr_max = parse_uf2(file_path)
            except UF2Error as e:
                raise PicotoolError('UF2 parse error: %s' % e)
            return self.verify_bytes(addr_min, bytes(image), progress=progress)
        else:
            file_data = self._read_bin_file(file_path)
            return self.verify_bytes(offset, file_data, progress=progress)

    # -- reboot -----------------------------------------------------------

    def reboot(self, to_bootsel=False, cpu=None, diagnostic_partition=None):
        """Reboot the device.

        to_bootsel=False (default): reboot into application mode.
        to_bootsel=True: reboot back into BOOTSEL mode (-u flag).
        cpu='arm' or 'riscv': select CPU architecture (RP2350 only,
            main.cpp:8553-8562). Adds REBOOT2_FLAG_REBOOT_TO_ARM (0x10)
            or REBOOT2_FLAG_REBOOT_TO_RISCV (0x20) to the flags.
        diagnostic_partition: partition number for diagnostic boot
            (main.cpp:8480, passed as param0 to REBOOT_TYPE_NORMAL).

        On RP2350 uses PC_REBOOT2 (the legacy PC_REBOOT does not
        actually leave BOOTSEL on RP2350). On RP2040 uses the legacy
        PC_REBOOT for application mode.

        Mirrors reboot_command::execute, main.cpp:8519-8593.

        For BOOTSEL mode on RP2350, main.cpp:8539-8549 uses:
            flags  = REBOOT2_FLAG_REBOOT_TYPE_BOOTSEL (0x2)
            param0 = 0  (no GPIO override)
            param1 = 0
            delay  = 500 ms
        """
        self.open()

        # cpu and diagnostic_partition require RP2350's PC_REBOOT2
        if cpu is not None and self.family != 'rp2350':
            raise PicotoolError('--cpu is only supported on RP2350')
        if diagnostic_partition is not None and self.family != 'rp2350':
            raise PicotoolError('--diagnostic is only supported on RP2350')

        if to_bootsel:
            if self.family == 'rp2350':
                # main.cpp:8539-8549
                flags = REBOOT2_FLAG_REBOOT_TYPE_BOOTSEL
                if cpu == 'arm':
                    flags |= REBOOT2_FLAG_REBOOT_TO_ARM
                elif cpu == 'riscv':
                    flags |= REBOOT2_FLAG_REBOOT_TO_RISCV
                self.conn.reboot2(
                    flags=flags,
                    delay_ms=500,
                    param0=0,
                    param1=0,
                )
            else:
                # RP2040: main.cpp:8563-8583 -- the C++ picotool loads a
                # tiny ARM program into SRAM that calls the ROM
                # reset_usb_boot() function via PC_EXEC. This requires
                # knowing the ROM function table address which varies
                # by ROM version. Document the limitation.
                raise PicotoolError(
                    'reboot --bootsel on RP2040 requires a debug probe '
                    'or holding the BOOTSEL button during reset. '
                    'Use reboot() for application mode instead.')
        else:
            if self.family == 'rp2350':
                # main.cpp:8540-8545
                flags = REBOOT2_FLAG_REBOOT_TYPE_NORMAL
                # main.cpp:8553-8562 -- CPU architecture selection
                if cpu == 'arm':
                    flags |= REBOOT2_FLAG_REBOOT_TO_ARM
                elif cpu == 'riscv':
                    flags |= REBOOT2_FLAG_REBOOT_TO_RISCV
                # main.cpp:8480 -- diagnostic partition as param0
                param0 = diagnostic_partition if diagnostic_partition is not None else 0
                self.conn.reboot2(
                    flags=flags,
                    delay_ms=500,
                    param0=param0,
                    param1=0,
                )
            else:
                self.conn.reboot(0, 0, 500)
