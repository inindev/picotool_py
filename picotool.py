#!/usr/bin/env python3
"""
picotool.py -- Python port of the C++ picotool CLI.

This file is the CLI veneer. All real work lives in picotool_lib.py;
this script is just argparse + ProgressBar wrapping + sys.exit error
handling. Run --help for the subcommand list.

For library use, import picotool_lib directly:

    from picotool_lib import Picotool
    with Picotool() as pt:
        pt.save(0x10000000, 0x1000, 'page.bin')
"""

import os
import sys

# ---------------------------------------------------------------------------
#  pyusb bootstrap
#
#  Prefer pyusb installed on the system Python; only fall back to a
#  per-tool venv if `import usb.core` fails. The venv lives in pyenv/
#  next to this script and is created lazily on first use.
# ---------------------------------------------------------------------------

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
VENV_DIR    = os.path.join(SCRIPT_DIR, 'pyenv')
VENV_PYTHON = os.path.join(VENV_DIR, 'bin', 'python3')

try:
    import usb.core  # noqa: F401
except ImportError:
    if os.environ.get('_PICOTOOL_VENV'):
        # We re-exec'd into the venv but pyusb is still missing.
        sys.exit('Error: pyusb not available even in the private venv. '
                 'Try deleting %s and rerunning.' % VENV_DIR)
    import subprocess
    if not os.path.exists(VENV_PYTHON):
        print('pyusb not installed; creating private venv in pyenv/ ...')
        try:
            subprocess.check_call([sys.executable, '-m', 'venv', VENV_DIR])
        except subprocess.CalledProcessError:
            sys.exit(
                'Error: python3-venv is not installed.\n'
                '  macOS:   brew install python3\n'
                '  Ubuntu:  sudo apt install python3-venv')
        print('Installing pyusb ...')
        subprocess.check_call(
            [VENV_PYTHON, '-m', 'pip', 'install', '-q', 'pyusb'])
    os.environ['_PICOTOOL_VENV'] = '1'
    os.execv(VENV_PYTHON,
             [VENV_PYTHON, os.path.abspath(__file__)] + sys.argv[1:])

# pyusb is now available (from system or from the private venv).
sys.path.insert(0, SCRIPT_DIR)

import argparse  # noqa: E402

from _wire import ProgressBar    # noqa: E402
from picotool_lib import (        # noqa: E402
    CommandFailure,
    ConnectionError,
    FLASH_START,
    Picotool,
    PicotoolError,
)


# ---------------------------------------------------------------------------
#  hex argument parser (mirrors picotool's cli::hex parser)
# ---------------------------------------------------------------------------

def parse_hex(s):
    """Parse a hex literal with or without 0x prefix."""
    s = s.strip()
    try:
        if s.lower().startswith('0x'):
            return int(s, 16)
        return int(s, 16)
    except ValueError:
        raise argparse.ArgumentTypeError('not a hex value: %s' % s)


# ---------------------------------------------------------------------------
#  UF2 family ID parser -- mirrors picotool's family_id value parser.
#  Accepts hex (0xe48bff59) or well-known names (rp2040, rp2350-arm-s, etc.)
# ---------------------------------------------------------------------------

_FAMILY_ALIASES = {
    'rp2040':        0xE48BFF56,
    'rp2350-arm-s':  0xE48BFF59,
    'rp2350-riscv':  0xE48BFF5A,
    'rp2350-arm-ns': 0xE48BFF5B,
    'absolute':      0xE48BFF57,
    'data':          0xE48BFF58,
}


def parse_family_id(s):
    """Parse a UF2 family ID: hex value or well-known name."""
    s = s.strip().lower()
    if s in _FAMILY_ALIASES:
        return _FAMILY_ALIASES[s]
    try:
        return int(s, 16) if s.startswith('0x') else int(s, 16)
    except ValueError:
        names = ', '.join(sorted(_FAMILY_ALIASES.keys()))
        raise argparse.ArgumentTypeError(
            'not a valid family ID: %s (try: %s)' % (s, names))


# ---------------------------------------------------------------------------
#  Common error handler
# ---------------------------------------------------------------------------

def _bail(e):
    sys.exit('error: %s' % e)


def _with_bar(prefix, fn):
    """Run `fn(progress_callback)` while displaying a ProgressBar with
    the given prefix. Always finishes the bar (newline) on exit."""
    bar = ProgressBar(prefix)
    try:
        result = fn(bar.progress)
        bar.update(100)
        return result
    finally:
        bar.finish()


# ---------------------------------------------------------------------------
#  Shared info display -- used by both device and file info paths
# ---------------------------------------------------------------------------

def _print_info(info):
    """Format and print a BinaryInfo object. Mirrors main.cpp:3641-3689
    (info_pair calls)."""
    if info is None:
        print('No binary info found.')
        return

    if info.program_name:
        print('Program Information')
        print(' name:          %s' % info.program_name)
    if info.program_version:
        print(' version:       %s' % info.program_version)
    if info.program_description:
        print(' description:   %s' % info.program_description)
    if info.program_url:
        print(' web site:      %s' % info.program_url)
    for feat in info.program_features:
        print(' features:      %s' % feat)
    if info.pico_board:
        print(' pico board:    %s' % info.pico_board)
    if info.sdk_version:
        print(' sdk version:   %s' % info.sdk_version)
    if info.boot2_name:
        print(' boot2:         %s' % info.boot2_name)
    if info.program_build_date:
        print(' build date:    %s' % info.program_build_date)
    for attr in info.build_attributes:
        print(' build attr:    %s' % attr)
    if info.binary_end:
        print(' binary end:    0x%08x' % info.binary_end)


# ---------------------------------------------------------------------------
#  CLI dispatch -- each function corresponds to one subcommand and is a
#  thin wrapper around a Picotool method.
# ---------------------------------------------------------------------------

def cli_info(args):
    try:
        if args.file:
            # Offline mode: parse binary_info from a file (no device needed).
            # Mirrors main.cpp info_command targeting a file (main.cpp:3227).
            pt = Picotool()
            info = pt.info_file(args.file)
            _print_info(info)
        else:
            # Device mode (default)
            with Picotool(serial=args.serial) as pt:
                info = _with_bar(
                    'Reading device: ',
                    lambda prog: pt.info(progress=prog),
                )
            _print_info(info)
    except (PicotoolError, ConnectionError, CommandFailure) as e:
        _bail(e)


def cli_save(args):
    try:
        with Picotool(serial=args.serial) as pt:
            if args.all:
                n = _with_bar(
                    'Saving file: ',
                    lambda prog: pt.save_all(args.file,
                                             family_id=args.family_id,
                                             progress=prog),
                )
            elif args.range_set:
                size = args.to_addr - args.from_addr
                n = _with_bar(
                    'Saving file: ',
                    lambda prog: pt.save(args.from_addr, size, args.file,
                                         progress=prog),
                )
            else:
                # Default: --program mode
                n = _with_bar(
                    'Saving file: ',
                    lambda prog: pt.save_program(args.file,
                                                 family_id=args.family_id,
                                                 progress=prog),
                )

            # main.cpp:827 -- -v/--verify: read back and compare
            if args.verify:
                try:
                    _with_bar(
                        'Verifying Flash: ',
                        lambda prog: pt.verify(args.file, progress=prog),
                    )
                    print('  OK')
                except PicotoolError as e:
                    print('  FAILED')
                    _bail(e)

        # main.cpp:4619
        print('Wrote %d bytes to %s' % (n, args.file))
    except (PicotoolError, ConnectionError, CommandFailure) as e:
        _bail(e)


def cli_erase(args):
    try:
        with Picotool(serial=args.serial) as pt:
            if args.range_set:
                n = _with_bar(
                    'Erasing: ',
                    lambda prog: pt.erase(args.from_addr,
                                          args.to_addr - args.from_addr,
                                          progress=prog),
                )
            else:
                # Default: --all mode (main.cpp:4714-4719)
                n = _with_bar(
                    'Erasing: ',
                    lambda prog: pt.erase_all(progress=prog),
                )
        # main.cpp:4737
        print('Erased %d bytes' % n)
    except (PicotoolError, ConnectionError, CommandFailure) as e:
        _bail(e)


def cli_load(args):
    offset = args.offset if args.offset is not None else FLASH_START
    try:
        with Picotool(serial=args.serial) as pt:
            _with_bar(
                'Loading into Flash: ',
                lambda prog: pt.load(args.file, offset=offset,
                                     file_type=args.type,
                                     family_id=args.family_id,
                                     progress=prog),
            )
            if args.verify:
                try:
                    _with_bar(
                        'Verifying Flash: ',
                        lambda prog: pt.verify(args.file, offset=offset,
                                               file_type=args.type,
                                               progress=prog),
                    )
                    # main.cpp:4922
                    print('  OK')
                except PicotoolError as e:
                    print('  FAILED')
                    _bail(e)
    except (PicotoolError, ConnectionError, CommandFailure) as e:
        _bail(e)


def cli_verify(args):
    """Standalone verify command. Mirrors verify_command (main.cpp:789-810)."""
    offset = args.offset if args.offset is not None else FLASH_START
    try:
        with Picotool(serial=args.serial) as pt:
            n = _with_bar(
                'Verifying Flash: ',
                lambda prog: pt.verify(args.file, offset=offset,
                                       file_type=args.type,
                                       progress=prog),
            )
        print('OK, %d bytes verified' % n)
    except PicotoolError as e:
        if 'verify failed' in str(e):
            print('FAILED')
        _bail(e)
    except (ConnectionError, CommandFailure) as e:
        _bail(e)


def cli_reboot(args):
    try:
        with Picotool(serial=args.serial) as pt:
            cpu = args.cpu if hasattr(args, 'cpu') and args.cpu else None
            diag = args.diagnostic if hasattr(args, 'diagnostic') and \
                args.diagnostic is not None else None
            if args.usb:
                pt.reboot(to_bootsel=True, cpu=cpu)
                # main.cpp:8588
                print('The device was rebooted into BOOTSEL mode.')
            else:
                pt.reboot(cpu=cpu, diagnostic_partition=diag)
                print('The device was rebooted into application mode.')
    except (PicotoolError, ConnectionError, CommandFailure) as e:
        _bail(e)


# ---------------------------------------------------------------------------
#  Top-level CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog='picotool',
        description='Python port of picotool.')
    _fh = parser.format_help
    parser.format_help = lambda: _fh() + '\n'

    # Global device selection. Mirrors main.cpp:612 (--ser).
    parser.add_argument('--ser', dest='serial', default=None,
                        help='Filter by serial number')

    sub = parser.add_subparsers(dest='command', metavar='')

    # -- info subcommand ---------------------------------------------------
    # Mirrors info_command::get_cli (main.cpp:724-741).
    # Supports device (default) or file target, plus compat flags.
    p_info = sub.add_parser('info',
                            help='Display information from device or file')
    p_info.add_argument('file', nargs='?', default=None,
                        help='File to inspect (.bin or .uf2) instead of device')
    # Compat flags: accepted but no-op since we always show all info.
    # main.cpp:727-733
    p_info.add_argument('-b', '--basic', action='store_true', default=True,
                        help='Include basic information (default, always shown)')
    p_info.add_argument('-l', '--build', action='store_true',
                        help='Include build attributes (always shown)')
    p_info.add_argument('-a', '--all', action='store_true',
                        help='Include all information (always shown)')

    # -- save subcommand ---------------------------------------------------
    # Mirrors save_command::get_cli (main.cpp:816-835).
    p_save = sub.add_parser('save', help='Save flash memory to a file')
    save_mode = p_save.add_mutually_exclusive_group()
    save_mode.add_argument('-p', '--program', action='store_true',
                           default=True,
                           help='Save program only (default)')
    save_mode.add_argument('-r', '--range', dest='range_set',
                           action='store_true',
                           help='Save a range of memory (requires from, to)')
    save_mode.add_argument('-a', '--all', action='store_true',
                           help='Save entire flash contents')
    p_save.add_argument('from_addr', metavar='from', type=parse_hex,
                        nargs='?', default=None,
                        help='The lower address bound in hex (--range)')
    p_save.add_argument('to_addr', metavar='to', type=parse_hex,
                        nargs='?', default=None,
                        help='The upper address bound in hex (--range)')
    p_save.add_argument('file', help='File to save to (.bin or .uf2)')
    # main.cpp:827 -- post-save verify
    p_save.add_argument('-v', '--verify', action='store_true',
                        help='Verify the data was saved correctly')
    # main.cpp:828-829 -- family ID override for UF2 output
    p_save.add_argument('--family', dest='family_id', type=parse_family_id,
                        default=None,
                        help='Specify the family ID for UF2 output')

    # -- erase subcommand --------------------------------------------------
    # Mirrors erase_command::get_cli (main.cpp:877-895).
    p_erase = sub.add_parser('erase', help='Erase flash sectors')
    p_erase.add_argument('-r', '--range', dest='range_set',
                         action='store_true',
                         help='Erase a range of memory (requires from, to)')
    p_erase.add_argument('from_addr', metavar='from', type=parse_hex,
                         nargs='?', default=None,
                         help='The lower address bound in hex (--range)')
    p_erase.add_argument('to_addr', metavar='to', type=parse_hex,
                         nargs='?', default=None,
                         help='The upper address bound in hex (--range)')

    # -- load subcommand ---------------------------------------------------
    # Mirrors load_command::get_cli (main.cpp:845-866).
    p_load = sub.add_parser('load', help='Load a BIN or UF2 file into flash')
    p_load.add_argument('file', help='Path to BIN or UF2 file')
    p_load.add_argument('-o', '--offset', type=parse_hex, default=None,
                        help='Load offset for BIN files (default 0x10000000)')
    p_load.add_argument('-v', '--verify', action='store_true',
                        help='Verify the data was written correctly')
    # main.cpp:703 -- explicit file type override
    p_load.add_argument('-t', '--type', choices=['uf2', 'bin'],
                        default=None,
                        help='Specify file type explicitly, ignoring extension')
    # main.cpp:849-850 -- family ID filter for UF2
    p_load.add_argument('--family', dest='family_id', type=parse_family_id,
                        default=None,
                        help='Specify the family ID of the UF2 file to load')

    # -- verify subcommand -------------------------------------------------
    # Mirrors verify_command (main.cpp:789-810).
    p_verify = sub.add_parser('verify',
                              help='Verify device contents match a file')
    p_verify.add_argument('file', help='File to compare against (.bin or .uf2)')
    p_verify.add_argument('-o', '--offset', type=parse_hex, default=None,
                          help='Load offset for BIN files (default 0x10000000)')
    p_verify.add_argument('-t', '--type', choices=['uf2', 'bin'],
                          default=None,
                          help='Specify file type explicitly, ignoring extension')

    # -- reboot subcommand -------------------------------------------------
    # Mirrors reboot_command::get_cli (main.cpp:1475-1484).
    p_reboot = sub.add_parser('reboot', help='Reboot the device')
    reboot_mode = p_reboot.add_mutually_exclusive_group()
    reboot_mode.add_argument('-a', '--application', action='store_true',
                             default=True,
                             help='Reboot into application mode (default)')
    reboot_mode.add_argument('-u', '--usb', action='store_true',
                             help='Reboot into BOOTSEL mode')
    # main.cpp:1481 -- CPU architecture selection (RP2350 only)
    p_reboot.add_argument('-c', '--cpu', choices=['arm', 'riscv'],
                          default=None,
                          help='Select arm | riscv CPU (RP2350 only)')
    # main.cpp:1480 -- diagnostic partition
    p_reboot.add_argument('-g', '--diagnostic', type=int, default=None,
                          metavar='partition',
                          help='Specify diagnostic partition (-3 to 15)')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Validate --range arguments for save/erase
    if args.command == 'save' and args.range_set:
        if args.from_addr is None or args.to_addr is None:
            sys.exit('error: --range requires from and to addresses')
    if args.command == 'erase' and args.range_set:
        if args.from_addr is None or args.to_addr is None:
            sys.exit('error: --range requires from and to addresses')

    {
        'info':   cli_info,
        'save':   cli_save,
        'erase':  cli_erase,
        'load':   cli_load,
        'verify': cli_verify,
        'reboot': cli_reboot,
    }[args.command](args)


if __name__ == '__main__':
    main()
