"""Throwaway fixture for Phase 3 real-PR static-analysis dogfooding.

Deliberately contains a couple of ruff-detectable issues so the PR
dogfood run has something real to classify against the diff. This file
(and the temporary PR/branch it's pushed on) is deleted once validation
is complete -- see the Phase 3 PR description for the dogfood results.
"""


def handle(user_input):
    try:
        return eval(user_input)
    except:
        pass
