from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from utilization import Utilization


class UtilizationTests(unittest.TestCase):
    def test_container_quota_and_unknown_first_sample(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name,value in {'memory.current':'128','memory.max':'256','cpu.max':'25000 100000','cpu.stat':'usage_usec 1000000'}.items():
                (root/name).write_text(value)
            meter=Utilization(root)
            with patch('utilization.time.monotonic',side_effect=[10,12]):
                first=meter.sample()
                (root/'cpu.stat').write_text('usage_usec 1250000')
                second=meter.sample()
            self.assertIsNone(first['cpu_percent'])
            self.assertEqual(second['cpu_percent'],50)
            self.assertEqual(second['memory_percent'],50)

    def test_unsupported_environment_is_not_zero_utilization(self):
        self.assertIsNone(Utilization('/nonexistent-cgroup').sample()['cpu_percent'])
