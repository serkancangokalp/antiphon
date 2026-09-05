"""Run the real bridge with the owner of a simulated host-identity fixture.

Only the automatic-identity Python doubles use this runner. Real CLI ancestry
tests and ordinary channel processes do not. The owner is a live Node listener,
not this short-lived Python child or an arbitrary constant.
"""
import os
import runpy
import sys
from unittest.mock import patch

owner_pid = int(sys.argv[1])
bridge_argv = sys.argv[2:]
sys.path.insert(0, os.path.dirname(os.path.abspath(bridge_argv[0])))
import peers

birth = peers._process_birth(owner_pid)
if birth is None:
    raise AssertionError("simulated host requires an observed listener birth")
owner = f"{owner_pid}:v1:{birth}"
sys.argv = bridge_argv
with patch.object(peers, "owner_key", return_value=owner):
    runpy.run_path(bridge_argv[0], run_name="__main__")
