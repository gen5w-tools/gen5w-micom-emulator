"""Offline checks for the tandem launcher wiring.

The launcher shells out to `emuctl` (external service); these tests stub that
boundary and never start a real emulator.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/micom'))
import tandem


class TandemTests(unittest.TestCase):
    def test_missing_emuctl_fails_fast_without_side_effects(self):
        argv = ['tandem.py', 'flash.bin', 'out']
        with mock.patch.object(tandem.shutil, 'which', return_value=None), \
             mock.patch.object(sys, 'argv', argv), \
             mock.patch.object(tandem.subprocess, 'run') as run, \
             mock.patch.object(tandem.subprocess, 'Popen') as popen:
            with self.assertRaises(SystemExit) as ctx:
                tandem.main()
        self.assertEqual(ctx.exception.code, 2)
        run.assert_not_called()
        popen.assert_not_called()

    def test_negative_steps_rejected(self):
        argv = ['tandem.py', 'flash.bin', 'out', '--steps', '-1']
        with mock.patch.object(tandem.shutil, 'which', return_value='/usr/bin/emuctl'), \
             mock.patch.object(sys, 'argv', argv):
            with self.assertRaises(SystemExit) as ctx:
                tandem.main()
        self.assertEqual(ctx.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
