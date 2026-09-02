"""Deterministic structural diff between a base and head
:class:`~patchfrog.contract_intelligence.function_signature.FunctionSignature`
-- produces :class:`~patchfrog.contract_intelligence.domain.BreakingCharacteristic`\\ s
(spec section 6). Never a BREAKING/SAFE verdict, never a numeric score --
see :data:`patchfrog.contract_intelligence.domain.BREAKING_CHARACTERISTICS`
for the subset actually treated as consumer-breaking.

Comparison is by parameter **name**, not positional index -- a pure
reordering of already-present, unchanged parameters is not flagged.
This is a deliberate, documented scope choice (see
``docs/contract-intelligence.md``'s Limitations): reasoning correctly
about positional-call-site breakage from reordering would need real
call-site argument-shape evidence this index doesn't have, and a wrong
guess here would be exactly the false-positive risk the product
principle (spec section 0) forbids.

``*args``/``**kwargs`` presence changes are recorded on neither
signature's comparison -- deliberately out of scope this milestone (see
Limitations) rather than guessed at.
"""

from __future__ import annotations

import re

from patchfrog.contract_intelligence.domain import BreakingCharacteristic
from patchfrog.contract_intelligence.function_signature import FunctionSignature, ParameterKind

_ADDRESSABLE_KINDS = (
    ParameterKind.POSITIONAL_ONLY,
    ParameterKind.POSITIONAL_OR_KEYWORD,
    ParameterKind.KEYWORD_ONLY,
)

#: Syntax-only "this annotation shape includes None" detector -- never a
#: resolved/imported-type check, purely textual (``Optional[...]`` or a
#: top-level ``... | None`` / ``None | ...``union member). False
#: negatives (an aliased Optional type this doesn't recognize) are
#: acceptable -- they just mean no RETURN_BECAME_OPTIONAL claim is made,
#: never a false positive.
_OPTIONAL_SHAPE_RE = re.compile(r"^Optional\[.*\]$|(^|\|\s*)None(\s*\||$)")


def _is_optional_shaped(annotation: str | None) -> bool:
    if annotation is None:
        return False
    return bool(_OPTIONAL_SHAPE_RE.search(annotation.strip()))


def diff_signatures(base: FunctionSignature, head: FunctionSignature) -> tuple[BreakingCharacteristic, ...]:
    characteristics: list[BreakingCharacteristic] = []

    base_by_name = {p.name: p for p in base.parameters if p.kind in _ADDRESSABLE_KINDS}
    head_by_name = {p.name: p for p in head.parameters if p.kind in _ADDRESSABLE_KINDS}

    for name in sorted(set(head_by_name) - set(base_by_name)):
        param = head_by_name[name]
        if param.has_default:
            characteristics.append(BreakingCharacteristic.OPTIONAL_PARAMETER_ADDED)
        else:
            characteristics.append(BreakingCharacteristic.REQUIRED_PARAMETER_ADDED)

    for _name in sorted(set(base_by_name) - set(head_by_name)):
        characteristics.append(BreakingCharacteristic.PARAMETER_REMOVED)

    for name in sorted(set(base_by_name) & set(head_by_name)):
        before, after = base_by_name[name], head_by_name[name]
        if before.has_default and not after.has_default:
            characteristics.append(BreakingCharacteristic.DEFAULT_REMOVED)
        elif not before.has_default and after.has_default:
            characteristics.append(BreakingCharacteristic.DEFAULT_ADDED)

    base_return = (base.return_annotation_text or "").strip()
    head_return = (head.return_annotation_text or "").strip()
    if base_return != head_return:
        base_optional = _is_optional_shaped(base.return_annotation_text)
        head_optional = _is_optional_shaped(head.return_annotation_text)
        if head_optional and not base_optional:
            characteristics.append(BreakingCharacteristic.RETURN_BECAME_OPTIONAL)
        elif base_optional and not head_optional:
            characteristics.append(BreakingCharacteristic.RETURN_BECAME_REQUIRED)
        else:
            characteristics.append(BreakingCharacteristic.RETURN_ANNOTATION_CHANGED)

    if base.is_async and not head.is_async:
        characteristics.append(BreakingCharacteristic.ASYNC_TO_SYNC)
    elif not base.is_async and head.is_async:
        characteristics.append(BreakingCharacteristic.SYNC_TO_ASYNC)

    return tuple(characteristics)
