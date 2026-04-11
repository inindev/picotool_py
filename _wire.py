"""
_wire.py -- Faithful Python port of picotool/picoboot_connection/

This file is a near-literal transliteration of picotool's C
picoboot_connection.c (and a small slice of picoboot_connection_cxx.cpp)
into Python on top of pyusb. Function names, parameter order, control
flow, and timeouts mirror the C source. Where Python idiom forces a
divergence (no static-state globals, exceptions instead of error
codes), the comment cites the C source line that's being matched.

References:
    pico-sdk/src/common/boot_picoboot_headers/include/boot/picoboot.h
    picotool/picoboot_connection/picoboot_connection.h
    picotool/picoboot_connection/picoboot_connection.c
    picotool/picoboot_connection/picoboot_connection_cxx.h
    picotool/picoboot_connection/picoboot_connection_cxx.cpp
"""

import struct
import time

import usb.core
import usb.util


# ---------------------------------------------------------------------------
#  Constants from picoboot.h and picoboot_connection.h
# ---------------------------------------------------------------------------

# picoboot_connection.h:19-25
VENDOR_ID_RASPBERRY_PI    = 0x2e8a
PRODUCT_ID_RP2040_USBBOOT = 0x0003
PRODUCT_ID_RP2350_USBBOOT = 0x000f

BOOTSEL_PIDS = {
    PRODUCT_ID_RP2040_USBBOOT: 'rp2040',
    PRODUCT_ID_RP2350_USBBOOT: 'rp2350',
}

# picoboot.h:26
PICOBOOT_MAGIC = 0x431fd10b

# picoboot.h:33-36 -- control requests on the PICOBOOT interface
PICOBOOT_IF_RESET      = 0x41   # OUT, no data
PICOBOOT_IF_CMD_STATUS = 0x42   # IN, 16-byte status struct

# picoboot.h:47-63 -- command IDs (top bit set => data flows IN)
PC_EXCLUSIVE_ACCESS = 0x01
PC_REBOOT           = 0x02
PC_FLASH_ERASE      = 0x03
PC_READ             = 0x84
PC_WRITE            = 0x05
PC_EXIT_XIP         = 0x06
PC_ENTER_CMD_XIP    = 0x07
PC_EXEC             = 0x08
PC_VECTORIZE_FLASH  = 0x09
PC_REBOOT2          = 0x0a
PC_GET_INFO         = 0x8b

# REBOOT2 flag values, from pico-sdk's boot/picoboot_constants.h
REBOOT2_FLAG_REBOOT_TYPE_NORMAL    = 0x0   # param0 = diagnostic partition
REBOOT2_FLAG_REBOOT_TYPE_BOOTSEL   = 0x2   # param0 = gpio_pin_number, param1 = flags
REBOOT2_FLAG_REBOOT_TYPE_RAM_IMAGE = 0x3
REBOOT2_FLAG_REBOOT_TO_ARM         = 0x10
REBOOT2_FLAG_REBOOT_TO_RISCV       = 0x20

# picoboot.h:119-123 -- exclusive access modes
NOT_EXCLUSIVE       = 0
EXCLUSIVE           = 1
EXCLUSIVE_AND_EJECT = 2

# picoboot.h:65-84 -- status codes
PICOBOOT_OK                       = 0
PICOBOOT_UNKNOWN_CMD              = 1
PICOBOOT_INVALID_CMD_LENGTH       = 2
PICOBOOT_INVALID_TRANSFER_LENGTH  = 3
PICOBOOT_INVALID_ADDRESS          = 4
PICOBOOT_BAD_ALIGNMENT            = 5
PICOBOOT_INTERLEAVED_WRITE        = 6
PICOBOOT_REBOOTING                = 7
PICOBOOT_UNKNOWN_ERROR            = 8
PICOBOOT_INVALID_STATE            = 9
PICOBOOT_NOT_PERMITTED            = 10
PICOBOOT_INVALID_ARG              = 11
PICOBOOT_BUFFER_TOO_SMALL         = 12
PICOBOOT_PRECONDITION_NOT_MET     = 13
PICOBOOT_MODIFIED_DATA            = 14
PICOBOOT_INVALID_DATA             = 15
PICOBOOT_NOT_FOUND                = 16
PICOBOOT_UNSUPPORTED_MODIFICATION = 17

# Mirrors status_code_strings in picoboot_connection_cxx.cpp:24-44
STATUS_CODE_STRINGS = {
    PICOBOOT_OK:                       'ok',
    PICOBOOT_BAD_ALIGNMENT:            'bad address alignment',
    PICOBOOT_INTERLEAVED_WRITE:        'interleaved write',
    PICOBOOT_INVALID_ADDRESS:          'invalid address',
    PICOBOOT_INVALID_CMD_LENGTH:       'invalid cmd length',
    PICOBOOT_INVALID_TRANSFER_LENGTH:  'invalid transfer length',
    PICOBOOT_REBOOTING:                'rebooting',
    PICOBOOT_UNKNOWN_CMD:              'unknown cmd',
    PICOBOOT_UNKNOWN_ERROR:            'unknown error',
    PICOBOOT_INVALID_STATE:            'invalid state',
    PICOBOOT_NOT_PERMITTED:            'permission failure',
    PICOBOOT_INVALID_ARG:              'invalid arg',
    PICOBOOT_BUFFER_TOO_SMALL:         'buffer too small',
    PICOBOOT_PRECONDITION_NOT_MET:     'precondition not met (pt not loaded)',
    PICOBOOT_MODIFIED_DATA:            'modified data (pt modified since load)',
    PICOBOOT_INVALID_DATA:             'data is invalid',
    PICOBOOT_NOT_FOUND:                'not found',
    PICOBOOT_UNSUPPORTED_MODIFICATION: 'unsupported modification',
}

# picoboot_connection.h:71-76 -- flash geometry
LOG2_PAGE_SIZE          = 8
PAGE_SIZE               = 1 << LOG2_PAGE_SIZE       # 256
FLASH_SECTOR_ERASE_SIZE = 4096

# Flash address range. picotool determines this per-model via
# get_memory_type(); for our use case the flash range is the same on
# RP2040 and RP2350 (16 MB max XIP window).
FLASH_START = 0x10000000
FLASH_END   = 0x11000000   # exclusive


# ---------------------------------------------------------------------------
#  Chunking + progress bar, ports of helpers in main.cpp
# ---------------------------------------------------------------------------

def calculate_chunk_size(size):
    """Port of main.cpp:348-350.

    Returns the per-iteration chunk size used for reads/writes/verifies.
    Aims for ~100 progress updates while staying aligned to a flash
    sector erase boundary."""
    chunk = (size + 99) // 100
    return (chunk + FLASH_SECTOR_ERASE_SIZE - 1) & ~(FLASH_SECTOR_ERASE_SIZE - 1)


# Mirrors main.cpp:113-118 (memory_names map). Used by ProgressBar to
# compute the prefix padding width so all bars line up.
_MEMORY_NAMES = ('RAM', 'Flash', 'XIP RAM', 'ROM')
_LONGEST_MEMORY_NAME = max(_MEMORY_NAMES, key=len)


class ProgressBar:
    """Port of struct progress_bar in main.cpp:4432-4465.

    Width 30, prefix padded to align with the longest possible
    'Loading into <memory>: ' string. Re-prints on '\\r' only when the
    integer percent changes."""

    def __init__(self, prefix, width=30):
        self.width = width
        # Picotool: extra_space = ("Loading into " + longest_mem + ": ").length() - new_prefix.length()
        template = 'Loading into %s: ' % _LONGEST_MEMORY_NAME
        pad_len = max(0, len(template) - len(prefix))
        self.prefix = prefix + ' ' * pad_len
        self.percent = -1
        self.update(0)

    def update(self, percent):
        if percent == self.percent:
            return
        self.percent = percent
        filled = (self.width * percent) // 100
        bar = '=' * filled + ' ' * (self.width - filled)
        # \r to overwrite, no newline; flush so it's visible mid-loop.
        print('%s[%s]  %d%%' % (self.prefix, bar, percent),
              end='\r', flush=True)

    def progress(self, dividend, divisor):
        # Mirrors main.cpp:4454-4456
        pct = 100 if divisor == 0 else (100 * dividend) // divisor
        self.update(pct)

    def finish(self):
        """Match picotool's progress_bar destructor: print a newline."""
        print()


# ---------------------------------------------------------------------------
#  Exceptions, mirroring picoboot_connection_cxx.h:14-27
# ---------------------------------------------------------------------------

class CommandFailure(Exception):
    """Mirrors picoboot::command_failure (picoboot_connection_cxx.h:14-22).

    Raised when a command was successfully transferred but the device
    reports a non-zero status code via PICOBOOT_IF_CMD_STATUS."""

    def __init__(self, code):
        self.code = code
        super().__init__(STATUS_CODE_STRINGS.get(code, '<unknown status %d>' % code))


class ConnectionError(Exception):
    """Mirrors picoboot::connection_error (picoboot_connection_cxx.h:24-27).

    Raised on a USB-level failure that wasn't recoverable as a status."""

    def __init__(self, libusb_code, msg=''):
        self.libusb_code = libusb_code
        super().__init__(msg or 'libusb error %s' % libusb_code)


# ---------------------------------------------------------------------------
#  bmRequestType constants used by control transfers
#
#  Bit layout (USB 2.0 9.3.1):
#    bit 7    direction:  0 = host->device, 1 = device->host
#    bits 6:5 type:       0 standard, 1 class, 2 vendor, 3 reserved
#    bits 4:0 recipient:  0 device, 1 interface, 2 endpoint, 3 other
# ---------------------------------------------------------------------------

LIBUSB_ENDPOINT_OUT          = 0x00
LIBUSB_ENDPOINT_IN           = 0x80
LIBUSB_REQUEST_TYPE_STANDARD = 0x00
LIBUSB_REQUEST_TYPE_VENDOR   = 0x40
LIBUSB_RECIPIENT_INTERFACE   = 0x01
LIBUSB_RECIPIENT_ENDPOINT    = 0x02
LIBUSB_REQUEST_GET_STATUS    = 0x00


# ---------------------------------------------------------------------------
#  Per-connection state, replacing the C statics in picoboot_connection.c:
#    static bool definitely_exclusive;       (line 24)
#    static enum { ... } xip_state;          (lines 25-29)
#    unsigned int interface, out_ep, in_ep;  (lines 58-60)
#    int one_time_bulk_timeout;              (line 292)
#    static int token = 1;                   (inside picoboot_cmd, line 298)
#
#  Bundled into a small object so multiple connections don't collide.
# ---------------------------------------------------------------------------

XIP_UNKNOWN  = 0
XIP_ACTIVE   = 1
XIP_INACTIVE = 2


class Connection:
    """Mirrors the picoboot::connection C++ wrapper, holding the device
    handle plus the per-instance state that the C code keeps in module
    statics."""

    def __init__(self, dev):
        self.dev = dev
        self.interface = None
        self.out_ep = None
        self.in_ep = None
        self.token = 1
        self.definitely_exclusive = False
        self.xip_state = XIP_UNKNOWN
        self.one_time_bulk_timeout = 0
        self._exclusive_held = False  # for context-manager cleanup

    # -- C++ connection ctor: picoboot_connection_cxx.h:30-34 --
    def open_and_reset(self, exclusive=True):
        """Equivalent of `connection(device, exclusive=true)` constructor.

        Claims the PICOBOOT interface, resets, and optionally takes
        exclusive access. Mirrors picoboot_connection_cxx.h:30-34."""
        self._claim_interface()
        # "do a device reset in case it was left in a bad state" (cxx.h:32)
        self.reset()
        if exclusive:
            self.exclusive_access(EXCLUSIVE)
            self._exclusive_held = True

    # -- C++ connection dtor: picoboot_connection_cxx.h:35-42 --
    def close(self):
        if self._exclusive_held:
            try:
                self.exclusive_access(NOT_EXCLUSIVE)
            except Exception:
                # "failed to restore exclusive access, so just reset"
                try:
                    self.reset()
                except Exception:
                    pass
            self._exclusive_held = False
        try:
            usb.util.release_interface(self.dev, self.interface)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # -- Interface claim, mirrors picoboot_connection.c:147-179 --
    def _claim_interface(self):
        cfg = self.dev.get_active_configuration()
        # picotool: "if config->bNumInterfaces == 1, interface=0 else interface=1"
        if cfg.bNumInterfaces == 1:
            ifnum = 0
        else:
            ifnum = 1
        intf = cfg[(ifnum, 0)]  # alt setting 0
        if intf.bInterfaceClass != 0xff or intf.bNumEndpoints != 2:
            raise ConnectionError(-1,
                'interface %d is not PICOBOOT (class=0x%02x, eps=%d)' %
                (ifnum, intf.bInterfaceClass, intf.bNumEndpoints))
        # picotool reads endpoint[0] as out, endpoint[1] as in
        eps = list(intf.endpoints())
        out_ep = eps[0].bEndpointAddress
        in_ep  = eps[1].bEndpointAddress
        if (out_ep & 0x80) or not (in_ep & 0x80):
            raise ConnectionError(-1,
                'endpoint directions wrong (out=0x%02x in=0x%02x)' %
                (out_ep, in_ep))
        # Detach kernel driver if present (Linux only). picotool relies on
        # the OS not auto-attaching one for vendor-class interfaces.
        try:
            if self.dev.is_kernel_driver_active(ifnum):
                self.dev.detach_kernel_driver(ifnum)
        except (NotImplementedError, usb.core.USBError):
            pass
        usb.util.claim_interface(self.dev, ifnum)
        self.interface = ifnum
        self.out_ep = out_ep
        self.in_ep = in_ep

    # ---- Control transfers ----------------------------------------------

    # is_halted, mirrors picoboot_connection.c:227-247
    def _is_halted(self, ep):
        bm = LIBUSB_RECIPIENT_ENDPOINT | LIBUSB_ENDPOINT_IN
        try:
            data = self.dev.ctrl_transfer(
                bmRequestType=bm,
                bRequest=LIBUSB_REQUEST_GET_STATUS,
                wValue=0,
                wIndex=ep,
                data_or_wLength=2,
                timeout=1000,
            )
        except usb.core.USBError:
            return False
        if len(data) != 2:
            return False
        return bool(data[0] & 1)

    # picoboot_reset, mirrors picoboot_connection.c:249-266
    def reset(self):
        if self._is_halted(self.in_ep):
            try:
                self.dev.clear_halt(self.in_ep)
            except usb.core.USBError:
                pass
        if self._is_halted(self.out_ep):
            try:
                self.dev.clear_halt(self.out_ep)
            except usb.core.USBError:
                pass
        bm = LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_RECIPIENT_INTERFACE  # 0x41
        try:
            self.dev.ctrl_transfer(
                bmRequestType=bm,
                bRequest=PICOBOOT_IF_RESET,
                wValue=0,
                wIndex=self.interface,
                data_or_wLength=0,
                timeout=1000,
            )
        except usb.core.USBError as e:
            raise ConnectionError(-1, 'IF_RESET failed: %s' % e)
        self.definitely_exclusive = False

    # picoboot_cmd_status, mirrors picoboot_connection.c:268-286
    def cmd_status(self):
        """Returns (token, status_code, cmd_id, in_progress)."""
        bm = (LIBUSB_REQUEST_TYPE_VENDOR
              | LIBUSB_RECIPIENT_INTERFACE
              | LIBUSB_ENDPOINT_IN)  # 0xc1
        data = self.dev.ctrl_transfer(
            bmRequestType=bm,
            bRequest=PICOBOOT_IF_CMD_STATUS,
            wValue=0,
            wIndex=self.interface,
            data_or_wLength=16,
            timeout=1000,
        )
        if len(data) != 16:
            raise ConnectionError(-1,
                'CMD_STATUS returned %d bytes, expected 16' % len(data))
        token, status_code, cmd_id, in_progress = struct.unpack_from(
            '<IIBB', bytes(data), 0)
        return token, status_code, cmd_id, in_progress

    # ---- Bulk command primitive -----------------------------------------

    # picoboot_cmd, mirrors picoboot_connection.c:294-387
    def _picoboot_cmd(self, cmd_id, args=b'', data_in_len=0, data_out=None):
        """Send one PICOBOOT command end-to-end and return any received data.

        Replicates the C picoboot_cmd() function literally:
            1. Build 32-byte command struct, write on out_ep (3000 ms)
            2. Data phase if dTransferLength != 0
                  IN  if cmd_id & 0x80, OUT otherwise
            3. 1-byte ACK on the OPPOSITE direction from the data phase
            4. Update internal xip_state / definitely_exclusive trackers

        Does NOT call cmd_status; the C version doesn't either. Status
        is only queried by callers (wrap_call) when this raises."""
        if data_out is not None and data_in_len:
            raise ValueError('cannot have both data_in and data_out')

        if data_out is not None:
            transfer_length = len(data_out)
        else:
            transfer_length = data_in_len

        if len(args) > 16:
            raise ValueError('args > 16 bytes')

        # Pack the 32-byte command struct.
        cmd_size = len(args)
        args_padded = args.ljust(16, b'\x00')
        token = self.token
        self.token += 1
        cmd_buf = struct.pack(
            '<IIBBHI16s',
            PICOBOOT_MAGIC,    # dMagic
            token,             # dToken
            cmd_id,            # bCmdId
            cmd_size,          # bCmdSize
            0,                 # _unused
            transfer_length,   # dTransferLength
            args_padded,       # args (union)
        )
        assert len(cmd_buf) == 32

        # 1. Send command packet (3000 ms, picoboot_connection.c:301)
        try:
            sent = self.dev.write(self.out_ep, cmd_buf, timeout=3000)
        except usb.core.USBError as e:
            raise ConnectionError(-1, 'send command: %s' % e)
        if sent != 32:
            raise ConnectionError(-1, 'short write on command (%d/32)' % sent)

        # Save state to restore in the no-state-change cases below.
        # Mirrors picoboot_connection.c:308-311.
        saved_xip_state = self.xip_state
        saved_exclusive = self.definitely_exclusive
        self.xip_state = XIP_UNKNOWN
        self.definitely_exclusive = False

        # picoboot_connection.c:312-316: timeout = one_time_bulk_timeout
        # if set, otherwise 10000.
        timeout = 10000
        if self.one_time_bulk_timeout:
            timeout = self.one_time_bulk_timeout
            self.one_time_bulk_timeout = 0

        # 2. Data phase
        received = b''
        if transfer_length:
            try:
                if cmd_id & 0x80:
                    arr = self.dev.read(self.in_ep, transfer_length,
                                        timeout=timeout)
                    received = bytes(arr)
                    if len(received) != transfer_length:
                        raise ConnectionError(-1,
                            'short read (%d/%d)' %
                            (len(received), transfer_length))
                else:
                    sent = self.dev.write(self.out_ep, data_out,
                                          timeout=timeout)
                    if sent != transfer_length:
                        raise ConnectionError(-1,
                            'short write data (%d/%d)' %
                            (sent, transfer_length))
            except usb.core.USBError as e:
                raise ConnectionError(-1, 'data phase: %s' % e)

        # 3. 1-byte ACK on the OPPOSITE direction (picoboot_connection.c:340-349)
        ack_timeout = timeout if transfer_length == 0 else 3000
        try:
            if cmd_id & 0x80:
                # read-style: ACK by sending 1 byte on out_ep
                self.dev.write(self.out_ep, b'\x00', timeout=ack_timeout)
            else:
                # write-style / no-data: ACK by reading up to 1 byte on in_ep
                self.dev.read(self.in_ep, 1, timeout=ack_timeout)
        except usb.core.USBError as e:
            raise ConnectionError(-1, 'ack: %s' % e)

        # 4. Update internal state trackers (picoboot_connection.c:351-384)
        if cmd_id == PC_EXIT_XIP:
            self.xip_state = XIP_INACTIVE
        elif cmd_id == PC_ENTER_CMD_XIP:
            self.xip_state = XIP_ACTIVE
        elif cmd_id in (PC_READ, PC_WRITE):
            self.xip_state = saved_xip_state
        # else: leave as XIP_UNKNOWN (already set above)

        if cmd_id == PC_EXCLUSIVE_ACCESS:
            self.definitely_exclusive = bool(args[0]) if args else False
        elif cmd_id in (PC_ENTER_CMD_XIP, PC_EXIT_XIP, PC_READ, PC_WRITE):
            self.definitely_exclusive = saved_exclusive
        # else: leave as False

        return received

    # ---- wrap_call: matches picoboot_connection_cxx.cpp:55-77 ----------
    def _wrap_call(self, fn):
        """Run fn(); on ConnectionError, query status to convert into a
        CommandFailure if the device has a meaningful status code, then
        reset to recover. Mirrors the wrap_call template."""
        try:
            return fn()
        except ConnectionError as e:
            try:
                _, status_code, _, _ = self.cmd_status()
            except Exception:
                raise e  # status query also failed; surface the original
            try:
                self.reset()
            except Exception:
                pass
            code = status_code if status_code else PICOBOOT_UNKNOWN_ERROR
            raise CommandFailure(code)

    # ---- High-level command wrappers, mirroring c++ connection:: ------

    def exclusive_access(self, exclusive):
        # picoboot_connection.c:389-397
        return self._wrap_call(lambda: self._picoboot_cmd(
            PC_EXCLUSIVE_ACCESS, args=bytes([exclusive])))

    def exit_xip(self):
        # picoboot_connection.c:399-411
        if self.definitely_exclusive and self.xip_state == XIP_INACTIVE:
            return  # "Skipping EXIT_XIP" optimization (line 400-403)
        self.xip_state = XIP_INACTIVE
        return self._wrap_call(lambda: self._picoboot_cmd(PC_EXIT_XIP))

    def read(self, addr, length):
        # picoboot_connection.c:501-520
        args = struct.pack('<II', addr, length)
        return self._wrap_call(lambda: self._picoboot_cmd(
            PC_READ, args=args, data_in_len=length))

    def flash_erase(self, addr, length):
        # picoboot_connection.c:470-479
        args = struct.pack('<II', addr, length)
        return self._wrap_call(lambda: self._picoboot_cmd(
            PC_FLASH_ERASE, args=args))

    def write(self, addr, data):
        # picoboot_connection.c:491-499
        args = struct.pack('<II', addr, len(data))
        return self._wrap_call(lambda: self._picoboot_cmd(
            PC_WRITE, args=args, data_out=bytes(data)))

    def reboot(self, pc=0, sp=0, delay_ms=500):
        """Mirrors picoboot_reboot (picoboot_connection.c:423-433).

        pc=0 means 'reset into regular boot path' (run the installed
        image). The dDelayMS field tells the device to wait that many
        milliseconds before resetting; picotool's default is 500. The
        delay gives the host time to receive the bulk-ACK before the
        device disappears from USB.

        On RP2350 picotool prefers reboot2() with REBOOT_TYPE_NORMAL --
        the legacy PC_REBOOT command does not actually leave BOOTSEL on
        RP2350. Use reboot2() for RP2350; this method is the RP2040
        fallback (matching main.cpp:8563-8565)."""
        args = struct.pack('<III', pc, sp, delay_ms)
        try:
            self._wrap_call(lambda: self._picoboot_cmd(PC_REBOOT, args=args))
        except (ConnectionError, CommandFailure):
            # Bulk ACK may race the actual reset; ignore.
            pass

    def reboot2(self, flags=REBOOT2_FLAG_REBOOT_TYPE_NORMAL, delay_ms=500,
                param0=0, param1=0):
        """Mirrors picoboot_reboot2 (picoboot_connection.c:435-443).

        Used by RP2350 reboots. Default args (NORMAL, delay 500, p0=p1=0)
        match picotool's main.cpp:8540-8545 for the plain `picotool reboot`
        invocation."""
        args = struct.pack('<IIII', flags, delay_ms, param0, param1)
        try:
            self._wrap_call(lambda: self._picoboot_cmd(PC_REBOOT2, args=args))
        except (ConnectionError, CommandFailure):
            pass

    # picoboot_memory_access::read_raw, main.cpp:2150-2201 (flash path).
    # We don't implement the RP2040 ROM trick or the unreadable-rom
    # path -- they're not exercised by `picotool save --range` against
    # flash, which is the only thing phase 2 needs.
    def read_memory(self, address, size):
        """Read `size` bytes starting at `address`. Handles flash
        page alignment by reading the surrounding aligned window and
        slicing the requested bytes out."""
        if FLASH_START <= address < FLASH_END:
            # main.cpp:2151-2153 -- exit XIP before any flash read
            self.exit_xip()
            # main.cpp:2186-2200 -- aligned vs unaligned flash read
            if (address & (PAGE_SIZE - 1)) == 0 and \
               ((address + size) & (PAGE_SIZE - 1)) == 0:
                # Both ends already 256-aligned: direct read.
                return self.read(address, size)
            else:
                aligned_start = address & ~(PAGE_SIZE - 1)
                aligned_end = (address + size + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1)
                tmp = self.read(aligned_start, aligned_end - aligned_start)
                offset = address - aligned_start
                return tmp[offset:offset + size]
        else:
            # RAM / ROM / etc -- direct read, bootrom handles it.
            return self.read(address, size)


# ---------------------------------------------------------------------------
#  Device discovery -- thin wrapper over picoboot_open_device's vid/pid
#  filtering (picoboot_connection.c:62-225)
# ---------------------------------------------------------------------------

def _get_serial(dev):
    try:
        return usb.util.get_string(dev, dev.iSerialNumber)
    except Exception:
        return None


def find_device(serial=None):
    """Find an RP2xxx in BOOTSEL mode. Returns (dev, family_string)."""
    try:
        candidates = list(usb.core.find(
            find_all=True,
            idVendor=VENDOR_ID_RASPBERRY_PI,
            custom_match=lambda d: d.idProduct in BOOTSEL_PIDS,
        ))
    except usb.core.NoBackendError:
        raise ConnectionError(-1,
            'libusb backend not found. Install libusb:\n'
            '  macOS:   brew install libusb\n'
            '  Linux:   sudo apt install libusb-1.0-0\n'
            '  Windows: install libusb via https://libusb.info')

    if serial is not None:
        candidates = [d for d in candidates if _get_serial(d) == serial]

    if not candidates:
        raise ConnectionError(-1,
            'No RP2040/RP2350 in BOOTSEL mode found. '
            'Hold BOOTSEL while plugging in USB.')
    if len(candidates) > 1:
        sers = ', '.join(_get_serial(d) or '?' for d in candidates)
        raise ConnectionError(-1,
            'Multiple BOOTSEL devices found (%s); specify --serial.' % sers)

    dev = candidates[0]
    return dev, BOOTSEL_PIDS[dev.idProduct]
