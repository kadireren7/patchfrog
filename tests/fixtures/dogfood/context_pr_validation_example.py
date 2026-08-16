"""Throwaway fixture for Phase 4 real-PR context-engine dogfooding.

Deliberately contains a bare-except finding, plus a caller and a test, so
the resulting Finding's context bundle has real caller/test context to
show. This file (and the temporary PR/branch it's pushed on) is deleted
once validation is complete -- see the Phase 4 PR description for results.
"""


def handle(user_input):
    try:
        return eval(user_input)
    except:
        pass


def dispatch(user_input):
    return handle(user_input)
