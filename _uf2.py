"""
_uf2.py -- UF2 file format parser and generator.

Ports the UF2 handling from picotool's elf2uf2/elf2uf2.cpp and the
UF2 loading path in main.cpp:2968 (build_rmap_uf2). Constants come
from pico-sdk/src/common/boot_uf2_headers/include/boot/uf2.h.

The UF2 (USB Flashing Format) encodes flash data as a series of
self-describing 512-byte blocks, each carrying a 256-byte payload
with a target address. This module handles:

    parse_uf2(path)    -- read a .uf2 file into (family_id, flat_image,
                          base_addr, end_addr)
    create_uf2(data, base_addr, family_id) -- generate UF2 bytes from
                          a flat memory image

References:
    pico-sdk/src/common/boot_uf2_headers/include/boot/uf2.h
    picotool/elf2uf2/elf2uf2.cpp
    picotool/elf2uf2/elf2uf2.h
    picotool/main.cpp (build_rmap_uf2, save_command, uf2_convert_command)
"""

import struct


# ---------------------------------------------------------------------------
#  Constants from pico-sdk boot/uf2.h
# ---------------------------------------------------------------------------

# uf2.h:28-30 -- block magic values
UF2_MAGIC_START0 = 0x0A324655
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END    = 0x0AB16F30

# uf2.h:32-36 -- flag bits
UF2_FLAG_NOT_MAIN_FLASH          = 0x00000001
UF2_FLAG_FILE_CONTAINER          = 0x00001000
UF2_FLAG_FAMILY_ID_PRESENT       = 0x00002000
UF2_FLAG_MD5_PRESENT             = 0x00004000
UF2_FLAG_EXTENSION_FLAGS_PRESENT = 0x00008000

# uf2.h:49-56 -- family IDs for Raspberry Pi chips
RP2040_FAMILY_ID          = 0xE48BFF56
ABSOLUTE_FAMILY_ID        = 0xE48BFF57
DATA_FAMILY_ID            = 0xE48BFF58
RP2350_ARM_S_FAMILY_ID    = 0xE48BFF59
RP2350_RISCV_FAMILY_ID    = 0xE48BFF5A
RP2350_ARM_NS_FAMILY_ID   = 0xE48BFF5B

# elf2uf2.h:20-21, uf2.h:39 -- page/block geometry
UF2_PAGE_SIZE  = 256     # payload bytes per UF2 block (1 << 8)
UF2_BLOCK_SIZE = 512     # total size of one UF2 block (sizeof(uf2_block))
UF2_DATA_SIZE  = 476     # data field in uf2_block (512 - 32 - 4)

# RP2350-E10 errata: absolute block extension marker
# main.cpp:2936 (check_abs_block)
UF2_EXTENSION_RP2_IGNORE_BLOCK = 0x9957E304

# Human-readable family names, matching main.cpp family_name_map
FAMILY_NAMES = {
    RP2040_FAMILY_ID:        'RP2040',
    ABSOLUTE_FAMILY_ID:      'RP2XXX (absolute)',
    DATA_FAMILY_ID:          'RP2XXX (data)',
    RP2350_ARM_S_FAMILY_ID:  'RP2350 ARM-S',
    RP2350_RISCV_FAMILY_ID:  'RP2350 RISC-V',
    RP2350_ARM_NS_FAMILY_ID: 'RP2350 ARM-NS',
}

# Family IDs that represent flashable application code.
# main.cpp uses the model's family_id to decide valid families; these
# are the ones that target main flash on RP2040/RP2350.
FLASH_FAMILIES = {
    RP2040_FAMILY_ID,
    RP2350_ARM_S_FAMILY_ID,
    RP2350_ARM_NS_FAMILY_ID,
    RP2350_RISCV_FAMILY_ID,
}


class UF2Error(Exception):
    """Raised for malformed or unsupported UF2 files."""


# ---------------------------------------------------------------------------
#  RP2350-E10 absolute block detection
#
#  Mirrors main.cpp:2936-2966 (check_abs_block). The absolute block is
#  a synthetic UF2 block used to work around RP2350-A2 silicon errata.
#  It has family_id=ABSOLUTE_FAMILY_ID, num_blocks=2, and 256 bytes of
#  0xef fill with an ignore-block extension marker.
# ---------------------------------------------------------------------------

def _is_abs_block(blk_data):
    """Return True if `blk_data` (512 bytes) is an RP2350-E10 absolute block.

    Checks the signature described in main.cpp:2936-2966:
      - magic numbers valid
      - flags has FAMILY_ID_PRESENT, optionally EXTENSION_FLAGS_PRESENT
      - payload_size == UF2_PAGE_SIZE
      - num_blocks == 2
      - file_size (family) == ABSOLUTE_FAMILY_ID
      - block_no == 0
      - data[0:256] all 0xef
      - if extension flags: data[256:260] == UF2_EXTENSION_RP2_IGNORE_BLOCK
    """
    ms0, ms1, flags, taddr, psize, bno, nblks, fam = \
        struct.unpack_from('<IIIIIIII', blk_data, 0)
    mend, = struct.unpack_from('<I', blk_data, 508)

    if ms0 != UF2_MAGIC_START0 or ms1 != UF2_MAGIC_START1 or \
       mend != UF2_MAGIC_END:
        return False
    if psize != UF2_PAGE_SIZE:
        return False
    if nblks != 2:
        return False
    if fam != ABSOLUTE_FAMILY_ID:
        return False
    if bno != 0:
        return False

    # Check required flag bits; only FAMILY_ID and EXTENSION_FLAGS allowed
    required = UF2_FLAG_FAMILY_ID_PRESENT
    allowed = required | UF2_FLAG_EXTENSION_FLAGS_PRESENT
    if (flags & required) != required:
        return False
    if (flags & ~allowed) != 0:
        return False

    # Data payload must be all 0xef (main.cpp:2948-2950)
    data = blk_data[32:32 + UF2_PAGE_SIZE]
    if data != bytes([0xef] * UF2_PAGE_SIZE):
        return False

    # If extension flags present, validate the ignore-block marker
    if flags & UF2_FLAG_EXTENSION_FLAGS_PRESENT:
        ext, = struct.unpack_from('<I', blk_data, 32 + UF2_PAGE_SIZE)
        if ext != UF2_EXTENSION_RP2_IGNORE_BLOCK:
            return False

    return True


# ---------------------------------------------------------------------------
#  UF2 parser -- mirrors build_rmap_uf2 (main.cpp:2968)
# ---------------------------------------------------------------------------

def parse_uf2(path, valid_families=None):
    """Parse a UF2 file and return (family_id, image, base_addr, end_addr).

    `image` is a flat bytearray covering [base_addr, end_addr), with
    gaps (addresses not present in the UF2) filled with 0xFF.

    `valid_families` restricts which family IDs are accepted. Defaults
    to FLASH_FAMILIES | {ABSOLUTE_FAMILY_ID} (all flashable families
    plus absolute). Pass a custom set to restrict further.

    Raises UF2Error on malformed data.

    Mirrors the loading path in main.cpp:2968 (build_rmap_uf2) plus
    the range-assembly logic in main.cpp:4503-4540 (save_command reads
    from the rmap; we do the equivalent inline here).
    """
    if valid_families is None:
        valid_families = FLASH_FAMILIES | {ABSOLUTE_FAMILY_ID}

    with open(path, 'rb') as f:
        raw = f.read()

    if len(raw) % UF2_BLOCK_SIZE != 0:
        raise UF2Error('file size %d is not a multiple of %d' %
                       (len(raw), UF2_BLOCK_SIZE))

    num_blocks = len(raw) // UF2_BLOCK_SIZE
    if num_blocks == 0:
        raise UF2Error('file is empty')

    # Collect (target_addr, payload_bytes) for each valid block.
    # Mirrors the per-block loop in build_rmap_uf2.
    family_id = None
    pages = {}       # target_addr -> bytes(payload)
    addr_min = None
    addr_max = None
    skipped = 0

    for i in range(num_blocks):
        blk = raw[i * UF2_BLOCK_SIZE:(i + 1) * UF2_BLOCK_SIZE]

        ms0, ms1, flags, taddr, psize, bno, nblks, fam = \
            struct.unpack_from('<IIIIIIII', blk, 0)
        mend, = struct.unpack_from('<I', blk, 508)

        # main.cpp:2984-2988 -- validate magic
        if ms0 != UF2_MAGIC_START0 or ms1 != UF2_MAGIC_START1 or \
           mend != UF2_MAGIC_END:
            raise UF2Error('block %d: bad magic' % i)

        # main.cpp:2990 -- skip NOT_MAIN_FLASH blocks
        if flags & UF2_FLAG_NOT_MAIN_FLASH:
            skipped += 1
            continue

        # main.cpp:2993 -- check FAMILY_ID_PRESENT
        if not (flags & UF2_FLAG_FAMILY_ID_PRESENT):
            skipped += 1
            continue

        # main.cpp:2936-2966 -- skip RP2350-E10 absolute block
        if _is_abs_block(blk):
            skipped += 1
            continue

        # main.cpp:2997-3002 -- validate family ID
        if fam not in valid_families:
            skipped += 1
            continue

        # main.cpp:3006-3010 -- track / check consistent family
        if family_id is None:
            family_id = fam
        elif fam != family_id and fam != ABSOLUTE_FAMILY_ID \
                and family_id != ABSOLUTE_FAMILY_ID:
            raise UF2Error('block %d: mixed family IDs (0x%08x vs 0x%08x)' %
                           (i, family_id, fam))

        # main.cpp:3013-3014 -- validate payload size
        if psize > UF2_DATA_SIZE:
            raise UF2Error('block %d: payload size %d > %d' %
                           (i, psize, UF2_DATA_SIZE))

        payload = blk[32:32 + psize]
        pages[taddr] = payload

        if addr_min is None or taddr < addr_min:
            addr_min = taddr
        end = taddr + psize
        if addr_max is None or end > addr_max:
            addr_max = end

    if not pages:
        raise UF2Error('file contains no valid data blocks')

    if family_id is None:
        raise UF2Error('no family ID found in file')

    # Assemble a flat image with 0xFF fill for gaps.
    total_size = addr_max - addr_min
    image = bytearray(b'\xff' * total_size)
    for taddr, payload in pages.items():
        off = taddr - addr_min
        image[off:off + len(payload)] = payload

    return family_id, image, addr_min, addr_max


# ---------------------------------------------------------------------------
#  UF2 generator -- mirrors pages2uf2 (elf2uf2.cpp:160-196)
# ---------------------------------------------------------------------------

def create_uf2(data, base_addr, family_id):
    """Generate UF2 file contents from a flat memory image.

    `data`      -- bytes/bytearray of flash contents
    `base_addr` -- target flash address for data[0]
    `family_id` -- UF2 family ID (e.g. RP2350_ARM_S_FAMILY_ID)

    Returns bytes containing the complete UF2 file.

    Mirrors the block generation in elf2uf2.cpp:160-196 (pages2uf2).
    Each 256-byte page becomes one 512-byte UF2 block. Short final
    pages are zero-padded to UF2_PAGE_SIZE.
    """
    data_len = len(data)
    if data_len == 0:
        raise UF2Error('cannot create UF2 from empty data')

    # Total number of 256-byte pages.
    # elf2uf2.cpp:162 -- num_blocks = total page count
    num_pages = (data_len + UF2_PAGE_SIZE - 1) // UF2_PAGE_SIZE

    out = bytearray()
    for page_num in range(num_pages):
        offset = page_num * UF2_PAGE_SIZE
        page_data = data[offset:offset + UF2_PAGE_SIZE]

        # Zero-pad the last page if shorter than UF2_PAGE_SIZE.
        # elf2uf2.cpp:184-186
        if len(page_data) < UF2_PAGE_SIZE:
            page_data = page_data + b'\x00' * (UF2_PAGE_SIZE - len(page_data))

        # Build the 512-byte UF2 block.
        # elf2uf2.cpp:170-181 -- fill common fields
        block = bytearray(UF2_BLOCK_SIZE)
        struct.pack_into('<IIIIIIII',
                         block, 0,
                         UF2_MAGIC_START0,                # magic_start0
                         UF2_MAGIC_START1,                # magic_start1
                         UF2_FLAG_FAMILY_ID_PRESENT,      # flags
                         base_addr + offset,              # target_addr
                         UF2_PAGE_SIZE,                   # payload_size
                         page_num,                        # block_no
                         num_pages,                       # num_blocks
                         family_id)                       # file_size / familyID
        block[32:32 + UF2_PAGE_SIZE] = page_data          # data payload
        struct.pack_into('<I', block, 508, UF2_MAGIC_END)  # magic_end
        out.extend(block)

    return bytes(out)
