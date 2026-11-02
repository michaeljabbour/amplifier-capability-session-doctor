"""Fictional JSONL must be unambiguous before a repair copy is permitted."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/session-doctor/scripts/session_doctor.py'
SPEC = importlib.util.spec_from_file_location('portable_session_doctor', SCRIPT)
doctor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(doctor)


class PortableJsonBoundaryTests(unittest.TestCase):
    def test_ambiguous_or_unrepresentable_numeric_lines_are_visible_and_not_repaired(self):
        cases = [
            '{"role":"assistant","role":"user","content":[]}',
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1","input":{"path":"a","path":"b"}}]}',
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1","input":{"number":NaN}}]}',
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1","input":{"number":Infinity}}]}',
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1","input":{"number":-Infinity}}]}',
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1","input":{"number":1e999}}]}',
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'source.jsonl'
            for raw in cases:
                with self.subTest(raw=raw):
                    path.write_text(raw + '\n', encoding='utf-8')
                    before = path.read_bytes()
                    original, messages, invalid = doctor.load(path)
                    self.assertEqual(original, before)
                    self.assertEqual(invalid, [1])
                    report = doctor.diagnose(messages, invalid)
                    self.assertFalse(report['supported_envelope'])
                    with self.assertRaises(ValueError):
                        doctor.repair_copy(messages, report)
                    self.assertEqual(path.read_bytes(), before)

    def test_actual_cli_refuses_ambiguous_input_without_creating_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.jsonl'
            target = Path(folder) / 'repair.jsonl'
            source.write_text('{"role":"assistant","content":[],"content":[{"type":"tool_use","id":"c1","input":{}}]}\n')
            before = source.read_bytes()
            result = subprocess.run([sys.executable, str(SCRIPT), str(source), '--repair-copy', str(target)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(target.exists())
            self.assertEqual(source.read_bytes(), before)

    def test_valid_numbers_unicode_and_literal_nonfinite_words_keep_existing_behavior(self):
        value = {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'c1', 'input': {'text': 'NaN is a word; café', 'fraction': 1.25, 'exponent': 1e4}}]}
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.jsonl'
            source.write_text(json.dumps(value, ensure_ascii=False) + '\n', encoding='utf-8')
            before, messages, invalid = doctor.load(source)
            self.assertEqual(messages, [value])
            self.assertEqual(invalid, [])
            fixed = doctor.repair_copy(messages, doctor.diagnose(messages, invalid))
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(fixed[0], value)
            self.assertTrue(fixed[1]['content'][0]['is_error'])


if __name__ == '__main__':
    unittest.main()
