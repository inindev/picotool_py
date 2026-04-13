# picotool_py

A Python port of the [picotool](https://github.com/raspberrypi/picotool) flash-management subset, speaking the PICOBOOT USB protocol directly via [pyusb](https://pyusb.github.io/pyusb/) + libusb. Designed to be **dropped into another project as a git submodule** and used either as a CLI (drop-in replacement for common `picotool` invocations) or as a Python library.

## Status

The implemented commands are byte-equivalent to the C++ tool on the same hardware.

| Command | Equivalent C++ invocation | Notes |
|---|---|---|
| `info [file]` | `picotool info [file]` | Displays program name, version, build date, SDK version, board, features, build attributes, binary end. Works from device or offline from a BIN/UF2 file |
| `save -p FILE` | `picotool save --program FILE` | Default mode; determines program extent from binary_info. BIN or UF2 output (auto-detected from extension) |
| `save -a FILE` | `picotool save --all FILE` | Saves entire flash; detects flash size via address mirroring |
| `save -r FROM TO FILE` | `picotool save --range FROM TO FILE` | Saves an address range. BIN or UF2 output |
| `erase` | `picotool erase` | Default: erases entire flash (detects size) |
| `erase -r FROM TO` | `picotool erase --range FROM TO` | Erases an address range; auto-rounds outward to 4 KB sector boundaries |
| `load FILE [-o OFFSET] [-v]` | `picotool load FILE [-o OFFSET] [-v]` | BIN or UF2 input (auto-detected). UF2 files use embedded target addresses; `-o` applies to BIN only |
| `load FILE -x` | `picotool load FILE --execute` | Load then reboot into the loaded program (REBOOT_TYPE_FLASH_UPDATE on RP2350) |
| `verify FILE [-o OFFSET]` | `picotool verify FILE [-o OFFSET]` | Standalone verify; BIN or UF2 input |
| `reboot` | `picotool reboot` | Reboots into application mode (PC_REBOOT2 on RP2350, PC_REBOOT on RP2040) |
| `reboot -u` | `picotool reboot --usb` | Reboots into BOOTSEL mode (RP2350 only via PC_REBOOT2) |
| `reboot -f` | `picotool reboot --force` | Force-reboot a running device (with pico_stdio_usb) into BOOTSEL without the physical button |
| `reboot -c arm\|riscv` | `picotool reboot --cpu arm\|riscv` | Select CPU architecture on RP2350 |

Additional CLI flags: `--ser` (device serial filter), `save --verify`, `save --family`, `load --type`, `load --family`, `reboot --diagnostic`.

**Out of scope** (use real picotool if you need these): ELF input, partition tables, signed images, `--no-overwrite`, `--update`, OTP read/write, pin info display, `config`, `seal`, `encrypt`, `link`.

## Requirements

- **Python 3.6+**
- **libusb 1.0** as a native library:
  - macOS: `brew install libusb`
  - Debian / Ubuntu / Raspberry Pi OS: `sudo apt install libusb-1.0-0` (often pre-installed)
  - Arch: `pacman -S libusb`
  - Fedora: `dnf install libusb1`
  - Windows: install via [https://libusb.info](https://libusb.info) and use Zadig to bind WinUSB to the RP2xxx BOOTSEL device.
- **pyusb** Python package. The bootstrap (see below) will use a system-installed pyusb if available, or create a private venv and `pip install pyusb` on first run.

You do **not** need the real C++ picotool installed.

## Installing as a submodule

In a parent project:

```sh
git submodule add <url-to-picotool_py> tools/picotool
git submodule update --init
```

The path can be anything; `tools/picotool` is a sensible default. From your project's Python tooling:

```python
import sys, os
PICOTOOL_DIR = os.path.join(os.path.dirname(__file__), 'tools', 'picotool')
sys.path.insert(0, PICOTOOL_DIR)

from picotool_lib import Picotool
```

Or, for shell-out from another script (matching the existing `picotool` CLI shape):

```python
subprocess.run([os.path.join(PICOTOOL_DIR, 'picotool.py'),
                'load', 'app.uf2', '-v'])
```

## CLI usage

```sh
# Hold BOOTSEL on the Pico, plug in USB, then:

# Display program info from device
./picotool.py info

# Display program info from a file (no device needed)
./picotool.py info app.uf2

# Save the program to a UF2 file
./picotool.py save app.uf2

# Save entire flash to a BIN file
./picotool.py save --all flash_dump.bin

# Save a specific range
./picotool.py save --range 0x10000000 0x10001000 firstpage.bin

# Erase entire flash
./picotool.py erase

# Erase a specific range
./picotool.py erase --range 0x100f0000 0x100f1000

# Load a UF2 file (addresses come from the UF2 blocks)
./picotool.py load app.uf2 -v

# Load a BIN file at a specific offset
./picotool.py load app.bin --offset 0x10000000 -v

# Verify device contents match a file
./picotool.py verify app.uf2

# Reboot into application mode
./picotool.py reboot

# Reboot back into BOOTSEL (RP2350 only)
./picotool.py reboot -u

# Select CPU on RP2350
./picotool.py reboot -c riscv

# Filter by serial number (when multiple devices connected)
./picotool.py --ser E66058388336A42F info

# Full help
./picotool.py --help
./picotool.py load --help
```

The CLI is a thin wrapper over `picotool_lib.py`. Output, exit codes, and progress-bar formatting match real picotool.

### pyusb / venv bootstrap

On first invocation `picotool.py` checks whether `import usb.core` works in the current Python interpreter:

- **Yes**: uses the system pyusb. No `pyenv/` directory is created.
- **No**: creates a private virtual environment in `pyenv/` next to the script, `pip install`s pyusb into it, and re-execs itself under that interpreter. Subsequent runs reuse the venv with no overhead.

Both branches result in `import usb.core` succeeding for the rest of the script. The `pyenv/` directory is `.gitignored`.

## Library usage

`picotool_lib.py` exposes a `Picotool` class. Use it as a context manager whenever possible -- opening the device is the slow part, and reusing one connection across many operations is much faster than shelling out per call.

```python
from picotool_lib import Picotool, PicotoolError

with Picotool() as pt:
    # display program info
    info = pt.info()
    if info:
        print(info.program_name, info.program_version)

    # save the program to a UF2 file
    pt.save_program('backup.uf2')

    # save entire flash
    pt.save_all('full_dump.bin')

    # save a specific range
    pt.save(0x10000000, 0x1000, 'page.bin')

    # erase entire flash
    pt.erase_all()

    # erase a sector (auto-rounds outward to 4 KB)
    pt.erase(0x100f0000, 0x1000)

    # load a UF2 file (addresses from UF2 blocks)
    pt.load('app.uf2')

    # load a BIN file at a specific offset
    pt.load('app.bin', offset=0x10040000)

    # load and immediately execute (reboot into the loaded program)
    pt.load('app.uf2', execute=True)

    # verify the load was successful
    pt.verify('app.uf2')

    # read raw bytes from flash
    data = pt.read(0x10000000, 256)

    # write raw bytes to flash
    pt.write(0x10040000, data)

    # verify raw bytes
    pt.verify_bytes(0x10040000, data)

    # detect flash size
    size = pt.guess_flash_size()

    # reboot into application mode
    pt.reboot()

    # reboot into BOOTSEL (RP2350)
    pt.reboot(to_bootsel=True)

    # reboot with CPU selection (RP2350)
    pt.reboot(cpu='riscv')

# Force a running device (not in BOOTSEL) into BOOTSEL mode.
# Requires firmware that links pico_stdio_usb.
pt = Picotool()
pt.force_into_bootsel()
pt.open()  # now in BOOTSEL, ready for commands
```

Offline file inspection (no device needed):

```python
pt = Picotool()
info = pt.info_file('app.uf2')
if info:
    print(info.program_name)
```

### Picotool class API

| Method | Returns | Description |
|---|---|---|
| `info(progress=None)` | `BinaryInfo` or `None` | Read binary_info metadata from device |
| `info_file(file_path)` | `BinaryInfo` or `None` | Parse binary_info from a BIN or UF2 file (no device) |
| `read(addr, size, progress=None)` | `bytes` | Read raw bytes from memory |
| `save(addr, size, file_path, file_type=None, family_id=None, progress=None)` | bytes written | Save a range to BIN or UF2 |
| `save_program(file_path, file_type=None, family_id=None, progress=None)` | bytes written | Save the program (extent from binary_info). BIN or UF2 |
| `save_all(file_path, file_type=None, family_id=None, progress=None)` | bytes written | Save entire flash. BIN or UF2 |
| `erase(addr, size, progress=None)` | bytes erased | Erase sectors covering a range (auto-rounds to 4 KB) |
| `erase_all(progress=None)` | bytes erased | Erase entire flash (detects size) |
| `write(addr, data, progress=None)` | bytes written | Write raw bytes to flash (erases sectors as needed) |
| `load(file_path, offset=FLASH_START, file_type=None, family_id=None, execute=False, progress=None)` | bytes loaded | Load a BIN or UF2 file; `execute=True` reboots into it |
| `verify(file_path, offset=FLASH_START, file_type=None, progress=None)` | bytes verified | Verify flash matches a file |
| `verify_bytes(addr, expected, progress=None)` | bytes verified | Verify flash matches raw bytes |
| `guess_flash_size()` | int | Detect flash size via address mirroring (0 if erased) |
| `reboot(to_bootsel=False, cpu=None, diagnostic_partition=None)` | None | Reboot device |
| `force_into_bootsel(wait=True)` | None | Force a running pico_stdio_usb device into BOOTSEL |
| `open()` / `close()` | None | Manual lifecycle (prefer context manager) |

`progress(current, total)` is an optional callback called periodically with byte counts during long operations. Pass `None` (the default) for silent operation, or wire up your own progress UI. The CLI uses `_wire.ProgressBar.progress` as the callback.

### BinaryInfo fields

The `BinaryInfo` object returned by `info()` and `info_file()` has these attributes, matching the binary_info IDs defined in `pico-sdk/src/common/pico_binary_info/include/pico/binary_info/structure.h`:

| Attribute | SDK ID |
|---|---|
| `program_name` | `BINARY_INFO_ID_RP_PROGRAM_NAME` (0x02031c86) |
| `program_version` | `BINARY_INFO_ID_RP_PROGRAM_VERSION_STRING` (0x11a9bc3a) |
| `program_build_date` | `BINARY_INFO_ID_RP_PROGRAM_BUILD_DATE_STRING` (0x9da22254) |
| `program_url` | `BINARY_INFO_ID_RP_PROGRAM_URL` (0x1856239a) |
| `program_description` | `BINARY_INFO_ID_RP_PROGRAM_DESCRIPTION` (0xb6a07c19) |
| `program_features` | `BINARY_INFO_ID_RP_PROGRAM_FEATURE` (0xa1f4b453) -- list of strings |
| `build_attributes` | `BINARY_INFO_ID_RP_PROGRAM_BUILD_ATTRIBUTE` (0x4275f0d3) -- list of strings |
| `pico_board` | `BINARY_INFO_ID_RP_PICO_BOARD` (0xb63cffbb) |
| `sdk_version` | `BINARY_INFO_ID_RP_SDK_VERSION` (0x5360b3ab) |
| `boot2_name` | `BINARY_INFO_ID_RP_BOOT2_NAME` (0x7f8882e1) |
| `binary_end` | `BINARY_INFO_ID_RP_BINARY_END` (0x68f465de) -- uint32 flash address |

### UF2 module

The `_uf2` module can be used standalone for UF2 file manipulation:

```python
from _uf2 import parse_uf2, create_uf2, RP2350_ARM_S_FAMILY_ID, FAMILY_NAMES

# Parse a UF2 file
family_id, image, addr_min, addr_max = parse_uf2('app.uf2')
print('Family: %s, range: 0x%08x-0x%08x' % (
    FAMILY_NAMES.get(family_id, '?'), addr_min, addr_max))

# Create a UF2 file from raw data
uf2_bytes = create_uf2(data, 0x10000000, RP2350_ARM_S_FAMILY_ID)
with open('output.uf2', 'wb') as f:
    f.write(uf2_bytes)
```

### Exceptions

| Exception | Raised when |
|---|---|
| `PicotoolError` | Argument errors, file-not-found, verify mismatch, UF2 parse errors |
| `CommandFailure` | The device returned a non-OK PICOBOOT status (carries `.code`) |
| `ConnectionError` | USB-level failure (no device found, libusb missing, etc.) |

All three live in `picotool_lib` and are re-exported for convenience. `CommandFailure` and `ConnectionError` come from `_wire.py` underneath.

## File layout

```
picotool_py/
|-- README.md          # this file
|-- LICENSE            # BSD-3-Clause
|-- _wire.py           # low-level USB transport + PICOBOOT protocol
|-- _uf2.py            # UF2 file format parser and generator
|-- _binary_info.py    # binary_info metadata parser
|-- picotool_lib.py    # high-level Picotool class
|-- picotool.py        # CLI veneer (argparse + dispatch + ProgressBar)
|-- tests/             # test suite (offline + hardware; run via tests/run_all.py)
`-- pyenv/             # auto-created on first run if system pyusb is missing; gitignored
```

The split is deliberate:
- `_wire.py` -- direct port of `picotool/picoboot_connection/picoboot_connection.{c,h}` plus a slice of `picoboot_connection_cxx.cpp`. Knows about USB endpoints, command packets, and the bulk-ACK handshake. Does not know what a "save" or a "load" is.
- `_uf2.py` -- UF2 file format handling. Ports the block structure, magic values, and family IDs from `pico-sdk/boot/uf2.h`, the parsing logic from `main.cpp:2968` (build_rmap_uf2), and the generation logic from `elf2uf2/elf2uf2.cpp:160` (pages2uf2). Includes RP2350-E10 absolute block detection.
- `_binary_info.py` -- binary_info metadata parser. Ports the header scanning from `main.cpp:2456` (find_binary_info), the address remapping from `main.cpp:2352` (remapped_memory_access), and the visitor extraction from `main.cpp:3575` (info_guts). Structure definitions from `pico-sdk/binary_info/structure.h`.
- `picotool_lib.py` -- high-level API (`Picotool` class) that mirrors the picotool CLI verbs. Pure Python operations on top of `_wire.py`, `_uf2.py`, and `_binary_info.py`. Silent (no `print()`) by design -- callers wire up their own output via the `progress` callback.
- `picotool.py` -- thin CLI veneer. Just argparse + dispatch + ProgressBar wrapping. All real work happens in `picotool_lib.py`.

## How the port relates to upstream picotool

Each module is a near-literal transliteration of a specific picotool source file. Functions cite the source file and line numbers they port from, so divergences are easy to spot.

| Our file | Ports |
|---|---|
| `_wire.py` | [`picoboot_connection/picoboot_connection.h`](https://github.com/raspberrypi/picotool/blob/master/picoboot_connection/picoboot_connection.h), [`picoboot_connection.c`](https://github.com/raspberrypi/picotool/blob/master/picoboot_connection/picoboot_connection.c), and a slice of [`picoboot_connection_cxx.cpp`](https://github.com/raspberrypi/picotool/blob/master/picoboot_connection/picoboot_connection_cxx.cpp) |
| `_uf2.py` | [`elf2uf2/elf2uf2.cpp`](https://github.com/raspberrypi/picotool/blob/master/elf2uf2/elf2uf2.cpp), [`elf2uf2/elf2uf2.h`](https://github.com/raspberrypi/picotool/blob/master/elf2uf2/elf2uf2.h), and UF2 block handling in [`main.cpp`](https://github.com/raspberrypi/picotool/blob/master/main.cpp) (build_rmap_uf2, save UF2 writer) |
| `_binary_info.py` | Binary info scanning and visitor in [`main.cpp`](https://github.com/raspberrypi/picotool/blob/master/main.cpp) (find_binary_info, info_guts), constants from [`pico-sdk binary_info/defs.h`](https://github.com/raspberrypi/pico-sdk/blob/master/src/common/pico_binary_info/include/pico/binary_info/defs.h) and [`structure.h`](https://github.com/raspberrypi/pico-sdk/blob/master/src/common/pico_binary_info/include/pico/binary_info/structure.h) |
| `picotool_lib.py` | The info / save / erase / load / verify / reboot command bodies in [`main.cpp`](https://github.com/raspberrypi/picotool/blob/master/main.cpp), plus `guess_flash_size` |
| `picotool.py` | The argparse-style CLI surface -- picotool's CLI uses `clipp`, but the option names and output format match |

The PICOBOOT command struct, command IDs, status codes, and control-transfer constants come from [pico-sdk's `boot/picoboot.h`](https://github.com/raspberrypi/pico-sdk/blob/master/src/common/boot_picoboot_headers/include/boot/picoboot.h) and [`boot/picoboot_constants.h`](https://github.com/raspberrypi/pico-sdk/blob/master/src/common/boot_picoboot_headers/include/boot/picoboot_constants.h). UF2 constants come from [`boot/uf2.h`](https://github.com/raspberrypi/pico-sdk/blob/master/src/common/boot_uf2_headers/include/boot/uf2.h).

## Limitations

Within the scope listed above, things to be aware of:

- **Tested primarily on RP2350.** The protocol code paths exercised on RP2040 are the ones picotool's RP2040 fallback uses, but RP2040 is not in regular CI. If you hit an RP2040-specific issue, it's possibly real.
- **`reboot --usb` is RP2350 only.** The C++ picotool implements RP2040 reboot-to-BOOTSEL by loading a small ARM stub into SRAM via PC_EXEC that calls the ROM `reset_usb_boot()` function. This requires knowing the ROM function table address which varies by ROM version. We don't implement this path -- on RP2040, use the physical BOOTSEL button.
- **Tested on macOS and Linux.** Windows *should* work -- pyusb supports it via libusb + WinUSB binding through Zadig -- but it has not been validated.
- **No flash cache.** Real picotool caches recent flash reads in `picoboot_memory_access::read_cached` to avoid re-fetching. We always go to the device. For typical one-shot CLI use this is invisible; for library callers doing many small reads of the same region, consider buffering at your level.
- **No retry on transient USB errors.** A spurious `LIBUSB_ERROR_PIPE` mid-operation surfaces as a `ConnectionError` rather than being retried. picotool catches and retries some of these.
- **`reboot --force` requires pico_stdio_usb.** The force-reboot path sends `RESET_REQUEST_BOOTSEL` to the device's USB reset interface, which is only present when the firmware links `pico_stdio_usb`. Firmware using UART-only stdio won't be detected.
- **No ELF support.** The UF2 and BIN formats are supported; ELF requires a full ELF parser which is out of scope. Convert ELF to UF2 using the SDK's `elf2uf2` tool first.
- **`info` doesn't display pin information or metadata blocks.** The `_binary_info.py` parser handles `ID_AND_STRING` and `ID_AND_INT` entry types only. Pin encoding (`PINS_WITH_FUNC`, `PINS64_WITH_FUNC`) and Picobin metadata blocks are not parsed.
- **`save --program` relies on `BINARY_INFO_ID_RP_BINARY_END`.** If the firmware doesn't embed this binary_info entry (older SDK versions, non-SDK toolchains), `save --program` will fail. Use `save --all` or `save --range` instead.
- **`guess_flash_size()` uses address mirroring heuristic.** It reads at decreasing power-of-2 offsets and looks for data that differs from the start of flash. This matches the C++ algorithm (main.cpp:2840-2860) but can be fooled by erased flash (returns 0) or unusual flash layouts.
- **Subprocess invocations pay USB enumeration cost per call.** If you're shelling out to `picotool.py` repeatedly from another script, prefer the library API (`from picotool_lib import Picotool`) and reuse a single connection. Each shell-out costs ~100-300 ms of device enumeration.

If any of these affect you, the cleanest fallback is to install the real C++ picotool alongside this one -- they coexist fine, and higher-level scripts can choose which to invoke per command.

## macOS BOOTSEL drive note

When you put a Pico into BOOTSEL on macOS, the OS auto-mounts the `RP2350` (or `RPI-RP2`) mass-storage drive in Finder. picotool talks to the PICOBOOT vendor interface directly and doesn't use that drive, but resetting the device causes a "Disk Not Ejected Properly" notification.

To suppress the notification, add this line to `/etc/fstab` via `sudo vifs`:

```
LABEL=RP2350 none auto rw,noauto
```

Important: the third field must be `auto`, not `msdos` -- the latter is silently ignored by macOS DiskArbitration. The Linux equivalent is a udev rule:

```
ACTION=="add", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="000f", ENV{UDISKS_IGNORE}="1"
```

## License

Ports of upstream picotool source -- original copyright Raspberry Pi (Trading) Ltd, BSD-3-Clause. The Python adaptation is offered under the same license.
