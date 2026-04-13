"""
_binary_info.py -- Parse Pico SDK binary_info metadata from flash/memory.

Ports the binary_info scanning and ID extraction from picotool's
main.cpp:2456-2495 (find_binary_info) and main.cpp:3556-3599
(info_guts visitor). Structure definitions come from
pico-sdk/src/common/pico_binary_info/include/pico/binary_info/.

Binary info is a metadata system embedded in Pico SDK binaries:
  - A 20-byte header (two markers + three pointers) is placed within
    the first 256 bytes of the binary (64 bytes on RP2040, after the
    256-byte boot2 stage).
  - The header points to an array of pointers to binary_info_t entries.
  - Each entry has a 4-byte core (type + tag) followed by type-specific
    fields.
  - Pointer values may be in RAM (runtime addresses) and require
    remapping back to flash offsets via the copy table.

References:
    pico-sdk/src/common/pico_binary_info/include/pico/binary_info/defs.h
    pico-sdk/src/common/pico_binary_info/include/pico/binary_info/structure.h
    picotool/main.cpp  (find_binary_info, info_guts, read_string)
"""

import struct


# ---------------------------------------------------------------------------
#  Constants from pico-sdk binary_info/defs.h
# ---------------------------------------------------------------------------

# defs.h:40-41
BINARY_INFO_MARKER_START = 0x7188EBF2
BINARY_INFO_MARKER_END   = 0xE71AA390

# ---------------------------------------------------------------------------
#  Constants from pico-sdk binary_info/structure.h
# ---------------------------------------------------------------------------

# structure.h:26-40 -- entry type codes
BINARY_INFO_TYPE_RAW_DATA                       = 1
BINARY_INFO_TYPE_SIZED_DATA                     = 2
BINARY_INFO_TYPE_BINARY_INFO_LIST_ZERO_TERMINATED = 3
BINARY_INFO_TYPE_BSON                           = 4
BINARY_INFO_TYPE_ID_AND_INT                     = 5
BINARY_INFO_TYPE_ID_AND_STRING                  = 6
BINARY_INFO_TYPE_BLOCK_DEVICE                   = 7
BINARY_INFO_TYPE_PINS_WITH_FUNC                 = 8
BINARY_INFO_TYPE_PINS_WITH_NAME                 = 9
BINARY_INFO_TYPE_NAMED_GROUP                    = 10
BINARY_INFO_TYPE_PINS64_WITH_FUNC               = 13
BINARY_INFO_TYPE_PINS64_WITH_NAME               = 14

# structure.h:124-125 -- pin encoding type identifiers
BI_PINS_ENCODING_RANGE = 1
BI_PINS_ENCODING_MULTI = 2

# structure.h:48 -- tag for Raspberry Pi defined entries
# BINARY_INFO_MAKE_TAG('R','P') = (ord('P') << 8) | ord('R') = 0x5052
BINARY_INFO_TAG_RASPBERRY_PI = 0x5052

# structure.h:50-60 -- well-known IDs for Raspberry Pi entries
BINARY_INFO_ID_RP_PROGRAM_NAME             = 0x02031C86
BINARY_INFO_ID_RP_PROGRAM_VERSION_STRING   = 0x11A9BC3A
BINARY_INFO_ID_RP_PROGRAM_BUILD_DATE_STRING = 0x9DA22254
BINARY_INFO_ID_RP_BINARY_END              = 0x68F465DE
BINARY_INFO_ID_RP_PROGRAM_URL             = 0x1856239A
BINARY_INFO_ID_RP_PROGRAM_DESCRIPTION     = 0xB6A07C19
BINARY_INFO_ID_RP_PROGRAM_FEATURE         = 0xA1F4B453
BINARY_INFO_ID_RP_PROGRAM_BUILD_ATTRIBUTE = 0x4275F0D3
BINARY_INFO_ID_RP_SDK_VERSION             = 0x5360B3AB
BINARY_INFO_ID_RP_PICO_BOARD              = 0xB63CFFBB
BINARY_INFO_ID_RP_BOOT2_NAME             = 0x7F8882E1

# Flash address range (shared with _wire.py but repeated here to avoid
# circular imports; these are architectural constants from
# model/addresses.h).
_FLASH_START = 0x10000000
_FLASH_END   = 0x12000000  # 32 MB max (RP2350); RP2040 is 16 MB


# ---------------------------------------------------------------------------
#  Buffer-based memory access helpers
# ---------------------------------------------------------------------------

def _buf_read(buf, base_addr, addr, size):
    """Read `size` bytes from `buf` at physical address `addr`.
    `base_addr` is the flash address of buf[0]. Returns None if OOR."""
    off = addr - base_addr
    if off < 0 or off + size > len(buf):
        return None
    return bytes(buf[off:off + size])


def _buf_read_u32(buf, base_addr, addr):
    """Read a little-endian uint32_t from buf at `addr`."""
    raw = _buf_read(buf, base_addr, addr, 4)
    if raw is None:
        return None
    return struct.unpack_from('<I', raw, 0)[0]


def _buf_read_string(buf, base_addr, addr, max_length=512):
    """Read a NUL-terminated string from buf. Mirrors main.cpp:2497-2506
    (read_string). Max 512 bytes, matching the C code. Clamps to
    available buffer if shorter than max_length (the C code zero-fills
    the read vector, which has the same effect)."""
    # Clamp to available bytes -- main.cpp:2499 uses read_vector with
    # zero_fill=true, so a short buffer just means fewer bytes to scan.
    off = addr - base_addr
    if off < 0 or off >= len(buf):
        return None
    avail = len(buf) - off
    read_len = min(max_length, avail)
    raw = _buf_read(buf, base_addr, addr, read_len)
    if raw is None:
        return None
    nul = raw.find(0)
    if nul >= 0:
        raw = raw[:nul]
    return raw.decode('utf-8', errors='replace')


# ---------------------------------------------------------------------------
#  Address remapping -- mirrors main.cpp:2352-2398 (remapped_memory_access)
#
#  The linker stores binary_info pointers using runtime (RAM) addresses,
#  but the actual data lives in flash. The copy table maps:
#     [dest_start, dest_end) -> source_addr
#  meaning runtime address `dest_start + N` maps to flash address
#  `source_addr + N`. This is the data copy table from crt0.S.
# ---------------------------------------------------------------------------

def _remap_addr(addr, copy_table):
    """Remap a runtime address to a flash address using the copy table.

    If the address is already in flash (0x10000000-0x12000000), return
    it as-is. Otherwise search the copy table entries (each is
    (source_addr_start, dest_addr_start, dest_addr_end)) and translate.

    Returns the remapped address, or 0 if unmappable.

    Mirrors the reverse_copy_mapping lookup in main.cpp:2375-2397.
    """
    # Already a flash address
    if _FLASH_START <= addr < _FLASH_END:
        return addr
    # Search the copy table: each entry is (src, dst_start, dst_end)
    # meaning flash[src + offset] == RAM[dst_start + offset]
    for src_start, dst_start, dst_end in copy_table:
        if dst_start <= addr < dst_end:
            return src_start + (addr - dst_start)
    return 0


# ---------------------------------------------------------------------------
#  Binary info header scan -- mirrors main.cpp:2456-2495 (find_binary_info)
# ---------------------------------------------------------------------------

def _find_header(buf, base_addr, family):
    """Scan for the binary_info header in `buf`.

    Returns (bi_addrs, copy_table) on success, or (None, None) if not found.

    bi_addrs: list of uint32_t pointers to binary_info_t entries
    copy_table: list of (source_start, dest_start, dest_end) tuples

    Mirrors main.cpp:2456-2495.

    Scanning rules (main.cpp:2462-2466):
      - RP2350: scan first 256 bytes from base_addr
      - RP2040: scan first 64 bytes, starting at base_addr + 0x100
        (skip the 256-byte boot2 stage if base is FLASH_START)
    """
    scan_base = base_addr
    # main.cpp:2462 -- max_dist is in uint32_t elements, not bytes.
    # read_vector<uint32_t>(base, max_dist) reads max_dist words.
    max_words = 256  # main.cpp:2462

    if family == 'rp2040':
        max_words = 64  # main.cpp:2464
        if base_addr == _FLASH_START:
            scan_base = base_addr + 0x100  # main.cpp:2465 -- skip boot2

    # Read max_words uint32 values (max_words * 4 bytes)
    raw = _buf_read(buf, base_addr, scan_base, max_words * 4)
    if raw is None:
        return None, None

    num_words = len(raw) // 4
    words = struct.unpack_from('<%dI' % num_words, raw, 0)

    # main.cpp:2468-2493 -- scan for MARKER_START, validate MARKER_END at i+4
    for i in range(num_words):
        if words[i] != BINARY_INFO_MARKER_START:
            continue
        if i + 4 >= num_words:
            continue
        if words[i + 4] != BINARY_INFO_MARKER_END:
            continue

        # Header found: extract the three pointers
        # defs.h layout:
        #   [i+0] MARKER_START
        #   [i+1] __binary_info_start (pointer array start)
        #   [i+2] __binary_info_end   (pointer array end, exclusive)
        #   [i+3] __address_mapping_table
        #   [i+4] MARKER_END
        bi_start = words[i + 1]
        bi_end = words[i + 2]
        cpy_table_ptr = words[i + 3]

        # main.cpp:2475-2478 -- validate: to > from, 4-aligned
        if bi_end <= bi_start:
            continue
        if (bi_start & 3) or (bi_end & 3):
            continue

        # Read the pointer array: each element is a uint32_t pointing
        # to a binary_info_t entry somewhere in flash or RAM.
        num_ptrs = (bi_end - bi_start) // 4
        ptrs_raw = _buf_read(buf, base_addr, bi_start, num_ptrs * 4)
        if ptrs_raw is None:
            # Pointers might be in RAM; try remapping after we parse
            # the copy table. For now, build the copy table first.
            pass

        # main.cpp:2480-2488 -- parse the copy (address mapping) table.
        # Terminated by a zero source_addr_start. Max 10 entries.
        copy_table = []
        ct_addr = cpy_table_ptr
        for _ in range(10):  # main.cpp:2488 -- arbitrary max
            entry_raw = _buf_read(buf, base_addr, ct_addr, 12)
            if entry_raw is None:
                break
            src, dst_start, dst_end = struct.unpack_from('<III', entry_raw, 0)
            if src == 0:
                break
            copy_table.append((src, dst_start, dst_end))
            ct_addr += 12

        # Now try reading the pointer array, remapping if needed.
        # The pointer array itself might be in RAM space.
        remapped_bi_start = _remap_addr(bi_start, copy_table)
        if remapped_bi_start == 0:
            continue
        ptrs_raw = _buf_read(buf, base_addr, remapped_bi_start, num_ptrs * 4)
        if ptrs_raw is None:
            continue

        bi_addrs = list(struct.unpack_from('<%dI' % num_ptrs, ptrs_raw, 0))
        return bi_addrs, copy_table

    return None, None


# ---------------------------------------------------------------------------
#  Binary info entry visitor -- mirrors main.cpp:3575-3599 (info_guts)
# ---------------------------------------------------------------------------

class BinaryInfo:
    """Parsed binary_info metadata from a Pico SDK binary.

    Attributes mirror the variables extracted in main.cpp:3556-3599:
        program_name        -- BINARY_INFO_ID_RP_PROGRAM_NAME
        program_version     -- BINARY_INFO_ID_RP_PROGRAM_VERSION_STRING
        program_build_date  -- BINARY_INFO_ID_RP_PROGRAM_BUILD_DATE_STRING
        program_url         -- BINARY_INFO_ID_RP_PROGRAM_URL
        program_description -- BINARY_INFO_ID_RP_PROGRAM_DESCRIPTION
        program_features    -- list of BINARY_INFO_ID_RP_PROGRAM_FEATURE strings
        build_attributes    -- list of BINARY_INFO_ID_RP_PROGRAM_BUILD_ATTRIBUTE strings
        pico_board          -- BINARY_INFO_ID_RP_PICO_BOARD
        sdk_version         -- BINARY_INFO_ID_RP_SDK_VERSION
        boot2_name          -- BINARY_INFO_ID_RP_BOOT2_NAME
        binary_end          -- BINARY_INFO_ID_RP_BINARY_END (uint32, flash address)
        pins                -- dict of {pin_number: [function_name, ...]}
                               from PINS_WITH_FUNC / PINS64_WITH_FUNC /
                               PINS_WITH_NAME / PINS64_WITH_NAME entries
    """

    def __init__(self):
        self.program_name = None
        self.program_version = None
        self.program_build_date = None
        self.program_url = None
        self.program_description = None
        self.program_features = []
        self.build_attributes = []
        self.pico_board = None
        self.sdk_version = None
        self.boot2_name = None
        self.binary_end = 0
        self.pins = {}  # {pin_number: [function_name, ...]}


# ---------------------------------------------------------------------------
#  Pin function name tables -- direct port of main.cpp:143-169.
#  Indexed by [function_number][pin_number].
# ---------------------------------------------------------------------------

# RP2040: 10 functions x 30 pins (main.cpp:143-154)
_PIN_FUNCS_RP2040 = [
    [""]*30,
    ["SPI0 RX","SPI0 CSn","SPI0 SCK","SPI0 TX","SPI0 RX","SPI0 CSn","SPI0 SCK","SPI0 TX","SPI1 RX","SPI1 CSn","SPI1 SCK","SPI1 TX","SPI1 RX","SPI1 CSn","SPI1 SCK","SPI1 TX","SPI0 RX","SPI0 CSn","SPI0 SCK","SPI0 TX","SPI0 RX","SPI0 CSn","SPI0 SCK","SPI0 TX","SPI1 RX","SPI1 CSn","SPI1 SCK","SPI1 TX","SPI1 RX","SPI1 CSn"],
    ["UART0 TX","UART0 RX","UART0 CTS","UART0 RTS","UART1 TX","UART1 RX","UART1 CTS","UART1 RTS","UART1 TX","UART1 RX","UART1 CTS","UART1 RTS","UART0 TX","UART0 RX","UART0 CTS","UART0 RTS","UART0 TX","UART0 RX","UART0 CTS","UART0 RTS","UART1 TX","UART1 RX","UART1 CTS","UART1 RTS","UART1 TX","UART1 RX","UART1 CTS","UART1 RTS","UART0 TX","UART0 RX"],
    ["I2C0 SDA","I2C0 SCL","I2C1 SDA","I2C1 SCL","I2C0 SDA","I2C0 SCL","I2C1 SDA","I2C1 SCL","I2C0 SDA","I2C0 SCL","I2C1 SDA","I2C1 SCL","I2C0 SDA","I2C0 SCL","I2C1 SDA","I2C1 SCL","I2C0 SDA","I2C0 SCL","I2C1 SDA","I2C1 SCL","I2C0 SDA","I2C0 SCL","I2C1 SDA","I2C1 SCL","I2C0 SDA","I2C0 SCL","I2C1 SDA","I2C1 SCL","I2C0 SDA","I2C0 SCL"],
    ["PWM0 A","PWM0 B","PWM1 A","PWM1 B","PWM2 A","PWM2 B","PWM3 A","PWM3 B","PWM4 A","PWM4 B","PWM5 A","PWM5 B","PWM6 A","PWM6 B","PWM7 A","PWM7 B","PWM0 A","PWM0 B","PWM1 A","PWM1 B","PWM2 A","PWM2 B","PWM3 A","PWM3 B","PWM4 A","PWM4 B","PWM5 A","PWM5 B","PWM6 A","PWM6 B"],
    ["SIO"]*30,
    ["PIO0"]*30,
    ["PIO1"]*30,
    ["","","","","","","","","","","","","","","","","","","","","CLOCK GPIN0","CLOCK GPOUT0","CLOCK GPIN1","CLOCK GPOUT1","CLOCK GPOUT2","CLOCK GPOUT3","","","",""],
    ["USB OVCUR DET","USB VBUS DET","USB VBUS EN"]*10,
]

# RP2350: 12 functions x 48 pins (main.cpp:156-169)
_PIN_FUNCS_RP2350 = [
    ["JTAG TCK","JTAG TMS","JTAG TDI","JTAG TDO","","","","","","","","","HSTX0","HSTX1","HSTX2","HSTX3","HSTX4","HSTX5","HSTX6","HSTX7"]+[""]*28,
    ["SPI0 RX","SPI0 CSn","SPI0 SCK","SPI0 TX","SPI0 RX","SPI0 CSn","SPI0 SCK","SPI0 TX","SPI1 RX","SPI1 CSn","SPI1 SCK","SPI1 TX","SPI1 RX","SPI1 CSn","SPI1 SCK","SPI1 TX","SPI0 RX","SPI0 CSn","SPI0 SCK","SPI0 TX","SPI0 RX","SPI0 CSn","SPI0 SCK","SPI0 TX","SPI1 RX","SPI1 CSn","SPI1 SCK","SPI1 TX","SPI1 RX","SPI1 CSn","SPI1 SCK","SPI1 TX","SPI0 RX","SPI0 CSn","SPI0 SCK","SPI0 TX","SPI0 RX","SPI0 CSn","SPI0 SCK","SPI0 TX","SPI1 RX","SPI1 CSn","SPI1 SCK","SPI1 TX","SPI1 RX","SPI1 CSn","SPI1 SCK","SPI1 TX"],
    ["UART0 TX","UART0 RX","UART0 CTS","UART0 RTS","UART1 TX","UART1 RX","UART1 CTS","UART1 RTS","UART1 TX","UART1 RX","UART1 CTS","UART1 RTS","UART0 TX","UART0 RX","UART0 CTS","UART0 RTS","UART0 TX","UART0 RX","UART0 CTS","UART0 RTS","UART1 TX","UART1 RX","UART1 CTS","UART1 RTS","UART1 TX","UART1 RX","UART1 CTS","UART1 RTS","UART0 TX","UART0 RX","UART0 CTS","UART0 RTS","UART0 TX","UART0 RX","UART0 CTS","UART0 RTS","UART1 TX","UART1 RX","UART1 CTS","UART1 RTS","UART1 TX","UART1 RX","UART1 CTS","UART1 RTS","UART0 TX","UART0 RX","UART0 CTS","UART0 RTS"],
    ["I2C0 SDA","I2C0 SCL","I2C1 SDA","I2C1 SCL"]*12,
    ["PWM0 A","PWM0 B","PWM1 A","PWM1 B","PWM2 A","PWM2 B","PWM3 A","PWM3 B","PWM4 A","PWM4 B","PWM5 A","PWM5 B","PWM6 A","PWM6 B","PWM7 A","PWM7 B","PWM0 A","PWM0 B","PWM1 A","PWM1 B","PWM2 A","PWM2 B","PWM3 A","PWM3 B","PWM4 A","PWM4 B","PWM5 A","PWM5 B","PWM6 A","PWM6 B","PWM7 A","PWM7 B","PWM8 A","PWM8 B","PWM9 A","PWM9 B","PWM10 A","PWM10 B","PWM11 A","PWM11 B","PWM8 A","PWM8 B","PWM9 A","PWM9 B","PWM10 A","PWM10 B","PWM11 A","PWM11 B"],
    ["SIO"]*48,
    ["PIO0"]*48,
    ["PIO1"]*48,
    ["PIO2"]*48,
    ["XIP CS1","CORESIGHT TRACECLK","CORESIGHT TRACEDATA0","CORESIGHT TDATA1","CORESIGHT TDATA2","CORESIGHT TDATA3","","","XIP CS1","","","","CLK GPIN","CLK GPOUT","CLK GPIN","CLK GPOUT","","","","XIP CS1","CLK GPIN","CLK GPOUT","CLK GPIN","CLK GPOUT","CLK GPOUT","CLK GPOUT"]+[""]*21+["XIP CS1"],
    ["USB OVCUR DET","USB VBUS DET","USB VBUS EN"]*16,
    ["","","UART0 TX","UART0 RX","","","UART1 TX","UART1 RX","","","UART1 TX","UART1 RX","","","UART0 TX","UART0 RX","","","UART0 TX","UART0 RX","","","UART1 TX","UART1 RX","","","UART1 TX","UART1 RX","","","UART0 TX","UART0 RX","","","UART0 TX","UART0 RX","","","UART1 TX","UART1 RX","","","UART1 TX","UART1 RX","","","UART0 TX","UART0 RX"],
]


def _decode_pins_with_func(encoding, is_64bit=False):
    """Decode a PINS_WITH_FUNC or PINS64_WITH_FUNC encoding into
    (pin_mask, function_number).

    Mirrors do_pins_func in main.cpp:2510-2544.

    Returns (mask, func) where mask is a bitmask of GPIO pin numbers
    and func is the function select index into the pin_functions table.
    """
    enc_type = encoding & 0x7          # bits [2:0]

    if is_64bit:
        bpp, pm, fp = 8, 0xFF, 8      # 64-bit: 8 bits/pin, mask 0xFF, first pin at bit 8
        func = (encoding >> 3) & 0x1F  # 5-bit function
        max_pins = 7
    else:
        bpp, pm, fp = 5, 0x1F, 7      # 32-bit: 5 bits/pin, mask 0x1F, first pin at bit 7
        func = (encoding >> 3) & 0xF   # 4-bit function
        max_pins = 5

    mask = 0
    if enc_type == BI_PINS_ENCODING_RANGE:
        plo = (encoding >> fp) & pm
        phi = (encoding >> (fp + bpp)) & pm
        for i in range(plo, phi + 1):
            mask |= 1 << i

    elif enc_type == BI_PINS_ENCODING_MULTI:
        last = -1
        work = encoding >> fp
        for _ in range(max_pins):
            cur = work & pm
            mask |= 1 << cur
            if cur == last:
                break
            last = cur
            work >>= bpp

    return mask, func


def _pin_func_name(func, pin, family):
    """Look up the function name for a pin number and function select.
    Returns the name string, or '' if unknown."""
    table = _PIN_FUNCS_RP2350 if family == 'rp2350' else _PIN_FUNCS_RP2040
    if func < len(table) and pin < len(table[func]):
        return table[func][pin]
    return ''


def parse_binary_info(buf, base_addr, family='rp2350'):
    """Parse binary_info from a memory buffer and return a BinaryInfo object.

    `buf`       -- bytes/bytearray of flash contents
    `base_addr` -- flash address of buf[0] (typically 0x10000000)
    `family`    -- 'rp2040' or 'rp2350' (affects scan window)

    Returns a BinaryInfo with all fields populated from the metadata,
    or None if no binary_info header was found.

    Mirrors the visitor pattern in main.cpp:3575-3599.
    """
    bi_addrs, copy_table = _find_header(buf, base_addr, family)
    if bi_addrs is None:
        return None

    info = BinaryInfo()

    for ptr in bi_addrs:
        # Remap the entry pointer to a flash address
        entry_addr = _remap_addr(ptr, copy_table)
        if entry_addr == 0:
            continue

        # Read the 4-byte core: type(u16) + tag(u16)
        # structure.h:67-70
        core_raw = _buf_read(buf, base_addr, entry_addr, 4)
        if core_raw is None:
            continue
        etype, etag = struct.unpack_from('<HH', core_raw, 0)

        # main.cpp:3576-3579 -- ID_AND_INT entries
        if etype == BINARY_INFO_TYPE_ID_AND_INT:
            # structure.h:88-92 -- core(4) + id(4) + value(4) = 12 bytes
            entry_raw = _buf_read(buf, base_addr, entry_addr, 12)
            if entry_raw is None:
                continue
            eid, evalue = struct.unpack_from('<Ii', entry_raw, 4)
            if etag != BINARY_INFO_TAG_RASPBERRY_PI:
                continue
            # main.cpp:3579
            if eid == BINARY_INFO_ID_RP_BINARY_END:
                info.binary_end = evalue & 0xFFFFFFFF

        # main.cpp:3581-3599 -- ID_AND_STRING entries
        elif etype == BINARY_INFO_TYPE_ID_AND_STRING:
            # structure.h:94-98 -- core(4) + id(4) + value_ptr(4) = 12 bytes
            entry_raw = _buf_read(buf, base_addr, entry_addr, 12)
            if entry_raw is None:
                continue
            eid, estr_ptr = struct.unpack_from('<II', entry_raw, 4)
            if etag != BINARY_INFO_TAG_RASPBERRY_PI:
                continue

            # Remap the string pointer and read the string
            str_addr = _remap_addr(estr_ptr, copy_table)
            if str_addr == 0:
                continue
            value = _buf_read_string(buf, base_addr, str_addr)
            if value is None:
                continue

            # main.cpp:3589-3598 -- dispatch by ID
            if eid == BINARY_INFO_ID_RP_PROGRAM_NAME:
                info.program_name = value
            elif eid == BINARY_INFO_ID_RP_PROGRAM_VERSION_STRING:
                info.program_version = value
            elif eid == BINARY_INFO_ID_RP_PROGRAM_BUILD_DATE_STRING:
                info.program_build_date = value
            elif eid == BINARY_INFO_ID_RP_PROGRAM_URL:
                info.program_url = value
            elif eid == BINARY_INFO_ID_RP_PROGRAM_DESCRIPTION:
                info.program_description = value
            elif eid == BINARY_INFO_ID_RP_PROGRAM_FEATURE:
                info.program_features.append(value)
            elif eid == BINARY_INFO_ID_RP_PROGRAM_BUILD_ATTRIBUTE:
                info.build_attributes.append(value)
            elif eid == BINARY_INFO_ID_RP_PICO_BOARD:
                info.pico_board = value
            elif eid == BINARY_INFO_ID_RP_SDK_VERSION:
                info.sdk_version = value
            elif eid == BINARY_INFO_ID_RP_BOOT2_NAME:
                info.boot2_name = value

        # main.cpp:2618-2620 -- PINS_WITH_FUNC (32-bit encoding)
        elif etype == BINARY_INFO_TYPE_PINS_WITH_FUNC:
            # structure.h:127-132 -- core(4) + pin_encoding(4) = 8 bytes
            entry_raw = _buf_read(buf, base_addr, entry_addr, 8)
            if entry_raw is None:
                continue
            encoding, = struct.unpack_from('<I', entry_raw, 4)
            mask, func = _decode_pins_with_func(encoding, is_64bit=False)
            for pin in range(64):
                if mask & (1 << pin):
                    name = _pin_func_name(func, pin, family)
                    if name:
                        info.pins.setdefault(pin, []).append(name)

        # main.cpp:2622-2624 -- PINS64_WITH_FUNC (64-bit encoding)
        elif etype == BINARY_INFO_TYPE_PINS64_WITH_FUNC:
            # structure.h:134-139 -- core(4) + pin_encoding(8) = 12 bytes
            entry_raw = _buf_read(buf, base_addr, entry_addr, 12)
            if entry_raw is None:
                continue
            encoding, = struct.unpack_from('<Q', entry_raw, 4)
            mask, func = _decode_pins_with_func(encoding, is_64bit=True)
            for pin in range(64):
                if mask & (1 << pin):
                    name = _pin_func_name(func, pin, family)
                    if name:
                        info.pins.setdefault(pin, []).append(name)

        # main.cpp:2621 -- PINS_WITH_NAME (32-bit mask + label)
        elif etype == BINARY_INFO_TYPE_PINS_WITH_NAME:
            # structure.h:141-145 -- core(4) + pin_mask(4) + label_ptr(4) = 12
            entry_raw = _buf_read(buf, base_addr, entry_addr, 12)
            if entry_raw is None:
                continue
            pin_mask, label_ptr = struct.unpack_from('<II', entry_raw, 4)
            label_addr = _remap_addr(label_ptr, copy_table)
            if label_addr == 0:
                continue
            label = _buf_read_string(buf, base_addr, label_addr)
            if label is None:
                continue
            # main.cpp:2680-2688 -- pipe-separated labels for consecutive pins
            parts = label.split('|')
            part_idx = 0
            for pin in range(32):
                if pin_mask & (1 << pin):
                    if part_idx < len(parts) and parts[part_idx]:
                        info.pins.setdefault(pin, []).append(parts[part_idx])
                    part_idx += 1

        # PINS64_WITH_NAME (64-bit mask + label)
        elif etype == BINARY_INFO_TYPE_PINS64_WITH_NAME:
            # structure.h:147-151 -- core(4) + pin_mask(8) + label_ptr(4) = 16
            entry_raw = _buf_read(buf, base_addr, entry_addr, 16)
            if entry_raw is None:
                continue
            pin_mask, label_ptr = struct.unpack_from('<QI', entry_raw, 4)
            label_addr = _remap_addr(label_ptr, copy_table)
            if label_addr == 0:
                continue
            label = _buf_read_string(buf, base_addr, label_addr)
            if label is None:
                continue
            parts = label.split('|')
            part_idx = 0
            for pin in range(64):
                if pin_mask & (1 << pin):
                    if part_idx < len(parts) and parts[part_idx]:
                        info.pins.setdefault(pin, []).append(parts[part_idx])
                    part_idx += 1

    return info
