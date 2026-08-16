from __future__ import annotations

from patchfrog.context.config import CONTEXT_ENGINE_VERSION, ContextConfig
from patchfrog.context.domain import ContextItemKind


def test_identical_configs_have_identical_fingerprints() -> None:
    assert ContextConfig().fingerprint() == ContextConfig().fingerprint()


def test_changing_max_tokens_changes_fingerprint() -> None:
    assert ContextConfig(max_tokens=4000).fingerprint() != ContextConfig(max_tokens=2000).fingerprint()


def test_changing_graph_depth_changes_fingerprint() -> None:
    assert ContextConfig(graph_depth=1).fingerprint() != ContextConfig(graph_depth=2).fingerprint()


def test_enabled_kinds_ordering_does_not_affect_fingerprint() -> None:
    a = ContextConfig(enabled_kinds=(ContextItemKind.CALLER, ContextItemKind.CALLEE))
    b = ContextConfig(enabled_kinds=(ContextItemKind.CALLEE, ContextItemKind.CALLER))
    assert a.fingerprint() == b.fingerprint()


def test_different_enabled_kinds_changes_fingerprint() -> None:
    a = ContextConfig(enabled_kinds=(ContextItemKind.CALLER,))
    b = ContextConfig(enabled_kinds=(ContextItemKind.CALLER, ContextItemKind.CALLEE))
    assert a.fingerprint() != b.fingerprint()


def test_fingerprint_incorporates_engine_version() -> None:
    import patchfrog.context.config as config_module

    fp_before = ContextConfig().fingerprint()
    original = config_module.CONTEXT_ENGINE_VERSION
    try:
        config_module.CONTEXT_ENGINE_VERSION = original + 1
        fp_after = ContextConfig().fingerprint()
    finally:
        config_module.CONTEXT_ENGINE_VERSION = original

    assert fp_before != fp_after


def test_wants_respects_enabled_kinds() -> None:
    config = ContextConfig(enabled_kinds=(ContextItemKind.CALLER,))
    assert config.wants(ContextItemKind.CALLER) is True
    assert config.wants(ContextItemKind.CALLEE) is False


def test_context_engine_version_is_a_positive_int() -> None:
    assert isinstance(CONTEXT_ENGINE_VERSION, int)
    assert CONTEXT_ENGINE_VERSION > 0
