"""Sandboxed verification-step adapters for Migration Verification (M8.4).

Every adapter here runs exclusively through
:class:`patchfrog.executable_verification.sandbox.VerificationSandbox` --
mirrors :mod:`patchfrog.executable_verification.pytest_adapter`'s shape.
Never a second, parallel subprocess-execution implementation.
"""
