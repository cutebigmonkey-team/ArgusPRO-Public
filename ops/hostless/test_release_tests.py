"""Regression for identically named modules and complete release-suite execution."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import release_tests


class ReleaseDiscoveryTests(unittest.TestCase):
    def test_each_suite_wins_over_an_already_present_foreign_test_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for name in release_tests.SUITES:
                directory=root/name;directory.mkdir();directory.joinpath('__init__.py').write_text('')
                other=root/('collect' if name!='collect' else 'drive')
                directory.joinpath('test_a_mutates.py').write_text(
                    'import sys,unittest\nsys.path.insert(0,'+repr(str(other))+')\n'
                    'class Mutation(unittest.TestCase):\n def test_import_completed(self):pass\n')
                directory.joinpath('test_same_name.py').write_text(
                    'import unittest\nclass Fixture(unittest.TestCase):\n'
                    ' def test_origin(self):\n  self.assertEqual('+repr(name)+',__import__("sys").argv[1])\n')
            script='''
import sys,unittest
from pathlib import Path
import release_tests
release_tests.OPS=Path(sys.argv[2])
# Reproduce the old condition: every suite is already on sys.path and the wrong
# test directory is ahead of the selected directory.
sys.path[:0]=[str(release_tests.OPS/p) for p in reversed(release_tests.SUITES)]
directory=release_tests.suite_path(sys.argv[1])
suite=unittest.TestLoader().discover(directory,top_level_dir=str(release_tests.OPS))
result=unittest.TextTestRunner().run(suite)
assert result.testsRun==2
assert sys.argv[1]+'.test_same_name' in sys.modules
raise SystemExit(not result.wasSuccessful())
'''
            env={**os.environ,'PYTHONPATH':str(Path(release_tests.__file__).parent)}
            for name in release_tests.SUITES:
                result=subprocess.run([sys.executable,'-c',script,name,str(root)],env=env,
                                      capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)

    def test_failure_exit_code_is_preserved_and_all_suites_still_run(self):
        run=Mock(side_effect=[SimpleNamespace(returncode=7),SimpleNamespace(returncode=0),SimpleNamespace(returncode=1)])
        self.assertEqual(release_tests.run_suites(run),7)
        self.assertEqual([call.args[0][-1] for call in run.call_args_list],list(release_tests.SUITES))
        self.assertTrue(all(call.kwargs['cwd']==release_tests.OPS.parent for call in run.call_args_list))

    def test_timeout_and_signal_fail_closed_without_omitting_later_suites(self):
        run=Mock(side_effect=[subprocess.TimeoutExpired('fixture',900),SimpleNamespace(returncode=-9),SimpleNamespace(returncode=0)])
        self.assertEqual(release_tests.run_suites(run),124);self.assertEqual(run.call_count,3)
        self.assertEqual(release_tests.run_suites(Mock(return_value=SimpleNamespace(returncode=0))),0)
