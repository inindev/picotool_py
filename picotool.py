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
#  CLI dispatch -- each function corresponds to one subcommand and is a
#  thin wrapper around a Picotool method.
# ---------------------------------------------------------------------------

def cli_save(args):
    size = args.to_addr - args.from_addr
    try:
        with Picotool() as pt:
            n = _with_bar(
                'Saving file: ',
                lambda prog: pt.save(args.from_addr, size, args.file,
                                     progress=prog),
            )
        # main.cpp:4619
        print('Wrote %d bytes to %s' % (n, args.file))
    except (PicotoolError, ConnectionError, CommandFailure) as e:
        _bail(e)


def cli_erase(args):
    try:
        with Picotool() as pt:
            n = _with_bar(
                'Erasing: ',
                lambda prog: pt.erase(args.from_addr,
                                      args.to_addr - args.from_addr,
                                      progress=prog),
            )
        # main.cpp:4737
        print('Erased %d bytes' % n)
    except (PicotoolError, ConnectionError, CommandFailure) as e:
        _bail(e)


def cli_load(args):
    offset = args.offset if args.offset is not None else FLASH_START
    try:
        with Picotool() as pt:
            _with_bar(
                'Loading into Flash: ',
                lambda prog: pt.load(args.file, offset=offset, progress=prog),
            )
            if args.verify:
                try:
                    _with_bar(
                        'Verifying Flash: ',
                        lambda prog: pt.verify(args.file, offset=offset,
                                               progress=prog),
                    )
                    # main.cpp:4922
                    print('  OK')
                except PicotoolError as e:
                    print('  FAILED')
                    _bail(e)
    except (PicotoolError, ConnectionError, CommandFailure) as e:
        _bail(e)


def cli_reboot(args):
    try:
        with Picotool() as pt:
            pt.reboot()
        # main.cpp:8588
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
    sub = parser.add_subparsers(dest='command', metavar='')

    # save subcommand. Mirrors save_command::get_cli (main.cpp:816-835)
    # for the --range option only; --program/--all/--family/--verify
    # are not implemented.
    p_save = sub.add_parser('save', help='Save flash memory to a file')
    p_save.add_argument('--range', '-r', dest='range_set', action='store_true',
                        help='Save a range of memory')
    p_save.add_argument('from_addr', metavar='from', type=parse_hex,
                        help='The lower address bound in hex')
    p_save.add_argument('to_addr', metavar='to', type=parse_hex,
                        help='The upper address bound in hex')
    p_save.add_argument('file', help='File to save to')

    # erase subcommand. Mirrors erase_command::get_cli (main.cpp:877-895)
    # for the --range option only; --all and --partition are not
    # implemented.
    p_erase = sub.add_parser('erase', help='Erase flash sectors')
    p_erase.add_argument('--range', '-r', dest='range_set', action='store_true',
                         help='Erase a range of memory')
    p_erase.add_argument('from_addr', metavar='from', type=parse_hex,
                         help='The lower address bound in hex')
    p_erase.add_argument('to_addr', metavar='to', type=parse_hex,
                         help='The upper address bound in hex')

    # load subcommand. Mirrors load_command::get_cli (main.cpp:845-866)
    # for BIN files with --offset only.
    p_load = sub.add_parser('load', help='Load a BIN file into flash')
    p_load.add_argument('file', help='Path to BIN file')
    p_load.add_argument('-o', '--offset', type=parse_hex, default=None,
                        help='Load offset (memory address; default 0x10000000)')
    p_load.add_argument('-v', '--verify', action='store_true',
                        help='Verify the data was written correctly')

    # reboot subcommand. Mirrors reboot_command::get_cli (main.cpp:1475-1484)
    # for the default (no flags) case.
    sub.add_parser('reboot', help='Reboot the device into application mode')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    {
        'save':   cli_save,
        'erase':  cli_erase,
        'load':   cli_load,
        'reboot': cli_reboot,
    }[args.command](args)


if __name__ == '__main__':
    main()
