"""Parser tests against sample R82 VSX output.  Run:  python -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collectors import commands  # noqa: E402

SAMPLE = open(os.path.join(os.path.dirname(__file__), "sample_vsx_output.txt")).read()


class TestParseAll(unittest.TestCase):
    def setUp(self):
        self.d = commands.parse_all(SAMPLE)

    def test_sections(self):
        self.assertEqual(set(self.d["sections_seen"]),
                         {name for name, _ in commands.SECTIONS})

    def test_os(self):
        self.assertEqual(self.d["os"]["cpu_percent"], 37)
        self.assertEqual(self.d["os"]["memory_percent"], 68.5)
        self.assertEqual(self.d["os"]["disk_percent"], 29)

    def test_cpus_and_cores(self):
        self.assertEqual([c["usage"] for c in self.d["cpus"]], [95, 15, 12, 3])
        self.assertEqual([(c["id"], c["cpu"], c["connections"]) for c in self.d["cores"]],
                         [(0, "3", 310), (1, "2", 502)])

    def test_firewall_and_pstat(self):
        self.assertEqual(self.d["firewall"]["connections_current"], 812)
        self.assertEqual(self.d["firewall"]["drops_total"], 15355)
        self.assertEqual(self.d["kernel_memory"]["kernel_memory_used_percent"], 9)
        self.assertEqual(self.d["kernel_memory"]["watermark_status"], "below watermark")
        self.assertEqual(self.d["cluster"]["members"], [])

    def test_vsx(self):
        vs = {v["id"]: v for v in self.d["vsx"]["systems"]}
        self.assertTrue(self.d["vsx"]["is_vsx"])
        self.assertEqual(vs[1]["name"], "VGW-Corp")
        self.assertEqual(vs[1]["connections"], 610)
        self.assertEqual(vs[1]["cpu_pct"], 94.0)
        self.assertEqual(vs[2]["cpu_pct"], 4.5)
        self.assertIsNone(vs[0]["cpu_pct"])

    def test_interfaces_and_rates(self):
        names = [i["name"] for i in self.d["interfaces"]]
        self.assertEqual(names, ["eth0", "eth1", "wrp128"])
        prev = commands.parse_all(SAMPLE.replace("98765432", "98665432"))
        commands.add_rates(self.d, prev, 10)
        self.assertEqual(self.d["interfaces"][0]["rx_bps"], 10000)

    def test_script_round_trip(self):
        script = commands.build_script()
        self.assertIn("vsx stat -l", script)
        self.assertEqual(commands.split_sections("noise\n@@@ os\nA\n@@@ end\n"), {"os": "A"})

    def test_non_vsx(self):
        d = commands.parse_all("@@@ vsx\nbash: vsx: command not found\n@@@ end\n")
        self.assertFalse(d["vsx"]["is_vsx"])


if __name__ == "__main__":
    unittest.main()
