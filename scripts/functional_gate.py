#!/usr/bin/env python3
"""Meaningful local functional checks; no provider calls or production activation."""
import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module

m=load('session_doctor',ROOT/'skills/session-doctor/scripts/session_doctor.py')
class Gate(unittest.TestCase):
    def test_repair_preserves_source_and_marks_no_execution(self):
        messages=[{'role':'assistant','content':[{'type':'tool_use','id':'c1','name':'read','input':{}}]}];before=copy.deepcopy(messages)
        fixed=m.repair_copy(messages,m.diagnose(messages));self.assertEqual(messages,before)
        result=fixed[1]['content'][0];self.assertTrue(result['is_error']);self.assertIn('no execution result',result['content'])
        self.assertFalse(any(i['severity'] in ('critical','high') for i in m.diagnose(fixed)['issues']))
    def test_malformed_unknown_and_duplicate_results_are_not_auto_repaired(self):
        for messages in [[{'event':'tool-start','data':{'id':'c1'}}],[{'role':'assistant','content':[{'type':'tool_use','id':'c1','input':{}},{'type':'tool_use','id':'c1','input':{}}]}]]:
            with self.assertRaises(ValueError):m.repair_copy(messages,m.diagnose(messages))
    def test_delayed_real_result_is_not_replaced(self):
        messages=[{'role':'assistant','content':[{'type':'tool_use','id':'c1','input':{}}]},{'role':'user','content':'interruption'},{'role':'user','content':[{'type':'tool_result','tool_use_id':'c1','content':'actual result'}]}]
        with self.assertRaises(ValueError):m.repair_copy(messages,m.diagnose(messages))

if __name__=='__main__':unittest.main(verbosity=2)
