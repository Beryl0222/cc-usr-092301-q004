import json
import unittest
from pathlib import Path
from src.contract import validate

class ContractTest(unittest.TestCase):
    def test_sample(self):
        data = json.loads((Path(__file__).parents[1] / "fixtures" / "event.json").read_text(encoding="utf-8"))
        self.assertEqual(validate(data), [])
