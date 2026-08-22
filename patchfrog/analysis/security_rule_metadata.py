"""Curated reason/remediation metadata for a small set of high-confidence,
well-understood static rules.

Deliberately a hand-written, closed lookup table keyed by ``rule_id`` --
never a per-row database column (``FindingModel`` doesn't persist
``raw_metadata`` at all, and adding a column for a handful of rules would
be exactly the "large schema rewrite" this refinement avoids). Applied at
presentation time (see :func:`patchfrog.publishing.queries.publishable_finding_from_static_finding`),
never at analysis time -- a rule not in this table simply gets no
remediation/reason claim beyond its own analyzer message, never a
fabricated one.

Every entry here has an established, mechanical failure mode (the rule
IS the mechanism -- "strcpy has no bounds check" is true unconditionally,
unlike "this eval() call is exploitable" which depends on unknown
caller context). ``impact`` is deliberately left unset for every entry:
none of these rules alone can establish real-world reachability/attacker
control, so claiming impact from the rule alone would be exactly the
"do not fabricate impact just because a static rule fired" anti-pattern
this refinement is meant to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SecurityRuleMetadata:
    #: The technical mechanism -- always true given the rule fired, not
    #: context-dependent.
    reason: str
    #: An actionable, root-cause remediation direction.
    remediation: str


_RULE_METADATA: dict[str, SecurityRuleMetadata] = {
    "patchfrog-python-eval-usage": SecurityRuleMetadata(
        reason="eval() executes its string argument as Python code at runtime.",
        remediation=(
            "Replace eval() with a safe, purpose-specific alternative (e.g. ast.literal_eval for "
            "literals) if the argument can ever come from outside this function's direct control."
        ),
    ),
    "patchfrog-python-bare-except": SecurityRuleMetadata(
        reason=(
            "A bare `except:` catches every exception, including KeyboardInterrupt and SystemExit, "
            "and discards the original error."
        ),
        remediation="Catch the specific exception type(s) actually expected here, and let anything else propagate.",
    ),
    "patchfrog-c-unsafe-strcpy": SecurityRuleMetadata(
        reason="strcpy() copies until the source's null terminator with no check against the destination buffer's size.",
        remediation="Use a bounded copy (e.g. snprintf, or strncpy with an explicit length matching the destination's real size).",
    ),
    "patchfrog-c-unsafe-gets": SecurityRuleMetadata(
        reason="gets() reads an unbounded line from stdin with no destination size check.",
        remediation="Use fgets() with the destination buffer's actual size instead.",
    ),
    "patchfrog-c-system-call": SecurityRuleMetadata(
        reason="system() passes its argument through a shell, so any content from outside this function's direct control can inject additional shell commands.",
        remediation=(
            "If any part of the command can come from outside this function's direct control, use an "
            "exec-family API that takes an argument list directly instead of a shell string."
        ),
    ),
    "F821": SecurityRuleMetadata(
        reason="The name is referenced but never defined in any reachable scope, so this raises NameError whenever executed.",
        remediation="Define the name before use, or fix the typo referencing an existing name.",
    ),
    "E722": SecurityRuleMetadata(
        reason=(
            "A bare `except:` catches every exception, including KeyboardInterrupt and SystemExit, "
            "and discards the original error."
        ),
        remediation="Catch the specific exception type(s) actually expected here, and let anything else propagate.",
    ),
}


def lookup(rule_id: str) -> SecurityRuleMetadata | None:
    return _RULE_METADATA.get(rule_id)
