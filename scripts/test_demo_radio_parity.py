"""Parity: the demo station catalog mirrors the real curated catalog.

demo/data/radio.js must serve exactly radio/stations.py STATION_CATALOG
(no demo-only providers such as BBC); the tour searches it, so drift
breaks the tour too. Run: python3 scripts/test_demo_radio_parity.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DUMP_JS = """const fs=require('fs'),vm=require('vm');
const ctx={window:{}}; ctx.window=ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync('demo/data/radio.js','utf8'),ctx);
console.log(JSON.stringify(ctx.FXROUTE_DEMO_RADIO.catalogStations.map(s=>s.id)));
"""


class RadioParityTest(unittest.TestCase):
    def test_catalog_matches_real(self):
        from radio.stations import STATION_CATALOG
        real_ids = [item["id"] for item in STATION_CATALOG]
        proc = subprocess.run(["node", "-e", DUMP_JS], capture_output=True,
                              text=True, cwd=ROOT, check=True)
        demo_ids = json.loads(proc.stdout)
        self.assertEqual(len(demo_ids), len(real_ids),
                         f"demo catalog drifts from real: {len(demo_ids)} vs {len(real_ids)}")
        self.assertEqual(set(demo_ids), set(real_ids),
                         f"missing={set(real_ids) - set(demo_ids)} extra={set(demo_ids) - set(real_ids)}")
        self.assertEqual(demo_ids, real_ids, "demo catalog order should follow the real catalog")


if __name__ == "__main__":
    unittest.main()
