#!/usr/bin/env python3
"""
run_all.py -- Run the picotool_py test suite.

Execution order (by blast radius):
  1. Offline tests (no hardware, no risk)
  2. Hardware tests (device required, flash region backed up/restored)
  3. Destructive tests (device leaves BOOTSEL, runs last)

Offline tests always run. Hardware tests prompt once for confirmation.
Destructive tests prompt separately. Individual test files are also
independently runnable: python3 tests/test_uf2.py

Exit code: 0 if all tests pass or skip, 1 if any fail.
"""

import importlib
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)
sys.path.insert(0, os.path.dirname(TEST_DIR))

from _harness import has_bootsel_device, has_real_picotool


def run_module(name):
    """Import and run a test module's main(), return its exit code."""
    mod = importlib.import_module(name)
    return mod.main()


def main():
    print('=' * 60)
    print('picotool_py test suite')
    print('=' * 60)
    print()

    exit_code = 0

    # --- Phase 1: offline tests (always run) ---
    print('--- offline tests (no hardware needed) ---')
    print()
    for mod in ('test_uf2', 'test_binary_info'):
        if run_module(mod) != 0:
            exit_code = 1
        print()

    # --- Phase 2: hardware tests ---
    have_device = has_bootsel_device()
    have_picotool = has_real_picotool()

    print('--- hardware tests ---')
    if not have_device:
        print('No BOOTSEL device found. Skipping hardware tests.')
        print('  (Hold BOOTSEL while plugging in USB to enable)')
        print()
    elif not have_picotool:
        print('Real picotool not in PATH. Skipping cross-validation tests.')
        print('  (Install picotool to enable full test coverage)')
        print()
    else:
        from _harness import get_test_region
        test_base, test_size = get_test_region()
        print('BOOTSEL device found. Real picotool found.')
        print('Tests use flash region 0x%08x-0x%08x (last %d KB).' % (
            test_base, test_base + test_size, test_size // 1024))
        print('The region is backed up before tests and restored after.')
        print()
        try:
            input('Press Enter to run hardware tests, or Ctrl-C to skip... ')
        except (KeyboardInterrupt, EOFError):
            print('\nSkipped.')
            print()
            # Skip to destructive tests prompt
            have_device = False

    if have_device and have_picotool:
        print()
        for mod in ('test_flash_ops', 'test_save_modes', 'test_info', 'test_cli'):
            if run_module(mod) != 0:
                exit_code = 1
            print()
    elif have_device:
        # Device but no real picotool -- still run what we can
        for mod in ('test_info', 'test_cli'):
            if run_module(mod) != 0:
                exit_code = 1
            print()

    # --- Phase 3: destructive tests ---
    print('--- destructive tests (device will leave BOOTSEL) ---')
    if not has_bootsel_device():
        print('No BOOTSEL device found. Skipping reboot test.')
        print()
    else:
        try:
            input('Press Enter to run reboot test, or Ctrl-C to skip... ')
        except (KeyboardInterrupt, EOFError):
            print('\nSkipped.')
            print()
        else:
            print()
            if run_module('test_reboot') != 0:
                exit_code = 1
            print()

    # --- Summary ---
    print('=' * 60)
    if exit_code == 0:
        print('All tests passed.')
    else:
        print('Some tests failed. See details above.')
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
