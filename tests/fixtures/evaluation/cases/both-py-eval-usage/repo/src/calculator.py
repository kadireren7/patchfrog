def evaluate_expression(expr: str) -> float:
    # eval() on a string that may come from an untrusted caller can
    # execute arbitrary code -- a real security bug, not a style nit.
    # Marked ground_truth_source: either so this case exercises the
    # static/AI overlap matrix's "both" bucket: PatchFrog's bundled
    # semgrep rule (patchfrog-python-eval-usage) and the AI oracle are
    # both expected to report it.
    return eval(expr)


def add(a: float, b: float) -> float:
    return a + b
