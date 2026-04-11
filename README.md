# picotool_py

A Python port of the [picotool](https://github.com/raspberrypi/picotool) flash-management subset, speaking the PICOBOOT USB protocol directly via [pyusb](https://pyusb.github.io/pyusb/) + libusb. Designed to be **dropped into another project as a git submodule** and used either as a CLI (drop-in replacement for the `picotool save / erase / load / reboot` invocations) or as a Python library.

## Status

The implemented commands are byte-equivalent to the C++ tool on the same hardware.

| Command | Equivalent C++ invocation | Notes |
|---|---|---|
| `save --range FROM TO FILE` | `picotool save --range FROM TO FILE` | BIN output only |
| `erase --range FROM TO`     | `picotool erase --range FROM TO`     | Auto-rounds outward to 4 KB sector boundaries |
| `load FILE -o OFFSET [-v]`  | `picotool load FILE --offset OFFSET [-v]` | BIN input only; `--offset` defaults to `0x10000000` |
| `reboot`                    | `picotool reboot`                    | Reboots into application mode (PC_REBOOT2 on RP2350, PC_REBOOT on RP2040) |

**Out of scope** (use real picotool if you need these): UF2/ELF input, `--all` / `--program` modes, partition tables, signed images, `--family`, `--execute`, `--no-overwrite`, `--update`, `--diagnostic`, `--cpu`, `--usb` reboot, OTP read/write, `info` display, `version`, signing.

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
                'save', '--range', '0x10000000', '0x10001000', 'page.bin'])
```

## CLI usage

```sh
# Hold BOOTSEL on the Pico, plug in USB, then:
./picotool.py save --range 0x10000000 0x10001000 firstpage.bin
./picotool.py erase --range 0x100f0000 0x100f1000
./picotool.py load app.bin --offset 0x10000000 -v
./picotool.py reboot
./picotool.py --help
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
    # save 4 KB of flash to a file
    pt.save(0x10000000, 0x1000, 'page.bin')

    # erase a sector (auto-rounds outward to 4 KB)
    pt.erase(0x100f0000, 0x1000)

    # load a BIN file at a specific offset
    pt.load('app.bin', offset=0x10040000)

    # verify the load was successful
    pt.verify('app.bin', offset=0x10040000)

    # reboot into application mode
    pt.reboot()
```

### Picotool class API

| Method | Returns | Raises |
|---|---|---|
| `save(addr, size, file_path, progress=None)` | bytes written | `PicotoolError`, `CommandFailure`, `ConnectionError` |
| `erase(addr, size, progress=None)` | bytes erased (after sector rounding) | as above |
| `load(file_path, offset=FLASH_START, progress=None)` | bytes loaded | as above |
| `verify(file_path, offset=FLASH_START, progress=None)` | bytes verified | `PicotoolError` on first byte mismatch |
| `reboot()` | None | as above |
| `open()` / `close()` | None | as above |

`progress(current, total)` is an optional callback called periodically with byte counts during long operations. Pass `None` (the default) for silent operation, or wire up your own progress UI. The CLI uses `_wire.ProgressBar.progress` as the callback.

### Exceptions

| Exception | Raised when |
|---|---|
| `PicotoolError` | Argument errors, file-not-found, verify mismatch |
| `CommandFailure` | The device returned a non-OK PICOBOOT status (carries `.code`) |
| `ConnectionError` | USB-level failure (no device found, libusb missing, etc.) |

All three live in `picotool_lib` and are re-exported for convenience. `CommandFailure` and `ConnectionError` come from `_wire.py` underneath.

## File layout

```
picotool_py/
|-- README.md          # this file
|-- LICENSE            # BSD-3-Clause
|-- _wire.py           # low-level USB transport + PICOBOOT protocol
|-- picotool_lib.py    # high-level Picotool class
|-- picotool.py        # CLI veneer (argparse + dispatch + ProgressBar)
`-- pyenv/             # auto-created on first run if system pyusb is missing; gitignored
```

The split is deliberate:
- `_wire.py` -- direct port of `picotool/picoboot_connection/picoboot_connection.{c,h}` plus a slice of `picoboot_connection_cxx.cpp`. Knows about USB endpoints, command packets, and the bulk-ACK handshake. Does not know what a "save" or a "load" is.
- `picotool_lib.py` -- high-level API (`Picotool` class) that mirrors the picotool CLI verbs. Pure Python operations on top of `_wire.py`. Silent (no `print()`) by design -- callers wire up their own output via the `progress` callback.
- `picotool.py` -- thin CLI veneer. Just argparse + dispatch + ProgressBar wrapping. All real work happens in `picotool_lib.py`.

## How the port relates to upstream picotool

Each module is a near-literal transliteration of a specific picotool source file. Functions cite the source file and line numbers they port from, so divergences are easy to spot.

| Our file | Ports |
|---|---|
| `_wire.py` | [`picoboot_connection/picoboot_connection.h`](https://github.com/raspberrypi/picotool/blob/master/picoboot_connection/picoboot_connection.h), [`picoboot_connection.c`](https://github.com/raspberrypi/picotool/blob/master/picoboot_connection/picoboot_connection.c), and a slice of [`picoboot_connection_cxx.cpp`](https://github.com/raspberrypi/picotool/blob/master/picoboot_connection/picoboot_connection_cxx.cpp) |
| `picotool_lib.py` | The save / erase / load / reboot command bodies in [`main.cpp`](https://github.com/raspberrypi/picotool/blob/master/main.cpp) |
| `picotool.py` | The argparse-style CLI surface -- picotool's CLI uses `clipp`, but the option names and output format match |

The PICOBOOT command struct, command IDs, status codes, and control-transfer constants come from [pico-sdk's `boot/picoboot.h`](https://github.com/raspberrypi/pico-sdk/blob/master/src/common/boot_picoboot_headers/include/boot/picoboot.h) and [`boot/picoboot_constants.h`](https://github.com/raspberrypi/pico-sdk/blob/master/src/common/boot_picoboot_headers/include/boot/picoboot_constants.h).

## Limitations and shortcomings

Within the scope listed above, things to be aware of:

- **Tested primarily on RP2350.** The protocol code paths exercised on RP2040 are the ones picotool's RP2040 fallback uses, but RP2040 is not in regular CI. If you hit an RP2040-specific issue, it's possibly real.
- **Tested on macOS and Linux.** Windows *should* work -- pyusb supports it via libusb + WinUSB binding through Zadig -- but it has not been validated.
- **No flash cache.** Real picotool caches recent flash reads in `picoboot_memory_access::read_cached` to avoid re-fetching. We always go to the device. For typical one-shot CLI use this is invisible; for library callers doing many small reads of the same region, consider buffering at your level.
- **No retry on transient USB errors.** A spurious `LIBUSB_ERROR_PIPE` mid-operation surfaces as a `ConnectionError` rather than being retried. picotool catches and retries some of these.
- **`--serial` device selection is implemented but lightly tested.** If you have more than one Pico in BOOTSEL on the same host, it should work but you may hit edge cases.
- **No `-f` / force-reboot-running-app-into-BOOTSEL path.** Real picotool can talk to a *running* app (one that links `pico_stdio_usb`) and ask it to reboot into BOOTSEL via a USB control transfer. We don't implement that -- the device must already be in BOOTSEL when our script runs.
- **Error messages are terser than picotool's.** PICOBOOT status codes are translated via a lookup table, but there's no per-command "what to try next" advice like the real tool sometimes prints.
- **The bootstrap can't detect a libusb / Python architecture mismatch.** If you have arm64 libusb installed and run an x86_64 Python (or vice versa, common on Apple Silicon under Rosetta), `import usb.core` will succeed but actual USB calls fail with cryptic backend errors. If you see something weird, run `python3 -c "import platform; print(platform.machine())"` and `file $(brew --prefix libusb)/lib/libusb-1.0.dylib` and check that they agree.
- **No `picotool info`-style introspection.** We can't display picobin metadata, family IDs, signed image status, etc. Use real picotool if you need to inspect a UF2 or running image.
- **`--family` flag is not honored** for save/load. The default family is implicit.
- **Subprocess invocations pay USB enumeration cost per call.** If you're shelling out to `picotool.py` repeatedly from another script, prefer the library API (`from picotool_lib import Picotool`) and reuse a single connection. Each shell-out costs ~100-300 ms of device enumeration.

If any of these affect you, the cleanest fallback is to install the real C++ picotool alongside this one -- they coexist fine, and `flash_manage_picotool.py`-style scripts can choose which to invoke per command.

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
