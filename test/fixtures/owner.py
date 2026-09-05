"""Explicit live owner data for registry fixtures, never a runtime override."""
import os

import peers


def current_process_owner():
    # These fixtures model an owner; they do not test discovering a CLI root.
    # Real PID + real birth keep liveness/recycling checks meaningful on CI.
    birth = peers._process_birth(os.getpid())
    if birth is None:
        raise AssertionError("fixture requires a readable current process birth")
    return f"{os.getpid()}:v1:{birth}"
