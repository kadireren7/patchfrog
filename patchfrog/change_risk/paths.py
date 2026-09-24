"""Path-only file classification. Deterministic, content-free.

Every rule here is deliberately conservative in the direction that costs
money, never in the direction that loses review: a path is only called
docs/generated/vendor/lockfile when it unambiguously is one. Anything
uncertain stays ``CODE`` and is reviewed normally.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from patchfrog.change_risk.domain import FileChangeClass

#: Narrower than the indexing denylist on purpose: ``build/``/``env/``
#: can be real source directories in some repositories, and skipping
#: review of real code is the expensive mistake here, not reviewing a
#: vendored file.
_VENDOR_DIRS = frozenset({"node_modules", "vendor", "third_party", "thirdparty", ".venv", "venv", "site-packages"})

_DOC_EXTENSIONS = frozenset({".md", ".markdown", ".mdx", ".rst", ".adoc", ".asciidoc"})
_DOC_BASENAMES = frozenset(
    {
        "license", "license.txt", "licence", "notice", "notice.txt", "authors", "authors.txt",
        "changelog", "changelog.txt", "changes", "changes.txt", "contributors", "codeowners",
        "readme", "readme.txt", "security.txt",
    }
)
_DOC_DIRS = frozenset({"docs", "doc", "documentation"})
_DOC_ASSET_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".txt"})

_LOCKFILES = frozenset(
    {
        "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
        "poetry.lock", "uv.lock", "pipfile.lock", "pdm.lock", "cargo.lock", "go.sum",
        "composer.lock", "gemfile.lock",
    }
)
_MANIFESTS = frozenset(
    {
        "package.json", "pyproject.toml", "setup.py", "setup.cfg", "pipfile", "go.mod",
        "cargo.toml", "gemfile", "composer.json", "pom.xml", "build.gradle", "build.gradle.kts",
    }
)
_REQUIREMENTS_RE = re.compile(r"^requirements([._-][\w.-]+)?\.(txt|in)$")

_GENERATED_SUFFIXES = (
    "_pb2.py", "_pb2.pyi", "_pb2_grpc.py", ".pb.go", ".pb.cc", ".pb.h", ".min.js", ".min.css",
    ".js.map", ".css.map", ".g.dart", ".freezed.dart",
)
_GENERATED_INFIXES = (".generated.", ".gen.")
_GENERATED_DIRS = frozenset({"__generated__", "generated", "dist"})

_MIGRATION_DIR_PATTERNS = (
    re.compile(r"(^|/)migrations/"),
    re.compile(r"(^|/)alembic/versions/"),
    re.compile(r"(^|/)db/migrate/"),
    re.compile(r"(^|/)migrate/"),
)
_SCHEMA_EXTENSIONS = frozenset({".proto", ".graphql", ".gql", ".avsc", ".prisma", ".sql"})
_SCHEMA_BASENAME_RE = re.compile(r"^(openapi|swagger|schema|schemas)(\.[\w-]+)?\.(ya?ml|json)$")
_MODEL_PATH_RE = re.compile(r"(^|/)(models?|schemas?|entities)(/|\.py$|\.ts$|\.go$)")

_CI_PATTERNS = (
    re.compile(r"^\.github/workflows/"),
    re.compile(r"^\.github/actions/"),
    re.compile(r"^\.gitlab-ci\.ya?ml$"),
    re.compile(r"^\.circleci/"),
    re.compile(r"(^|/)jenkinsfile$"),
    re.compile(r"^azure-pipelines\.ya?ml$"),
    re.compile(r"^\.buildkite/"),
)
_CONFIG_BASENAMES = frozenset(
    {"dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
     "alembic.ini", "makefile", ".dockerignore", "tox.ini", "nginx.conf"}
)
_CONFIG_EXTENSIONS = frozenset({".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml", ".env.example"})

_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]+$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$",
    re.IGNORECASE,
)

#: Security-sensitive *path tokens*. Matched against whole path tokens
#: (split on ``/ _ - .``), never substrings -- ``author.py`` is not
#: ``auth``. A path token is a scrutiny signal only, never a finding and
#: never on its own the reason for the highest tier.
_SECURITY_TOKENS = frozenset(
    {
        "auth", "authn", "authz", "authentication", "authorization", "oauth", "oauth2", "jwt",
        "login", "logout", "signin", "signup", "session", "sessions", "password", "passwords",
        "passwd", "credential", "credentials", "secret", "secrets", "token", "tokens", "crypto",
        "cryptography", "cipher", "encrypt", "encryption", "decrypt", "permission", "permissions",
        "acl", "rbac", "sso", "saml", "csrf", "xss", "sanitize", "sanitizer", "security",
        "sandbox", "keystore", "keyring", "apikey", "apikeys", "signature", "signatures",
        "webhook", "webhooks", "redaction", "redact",
    }
)
_TOKEN_SPLIT_RE = re.compile(r"[/_.\-]+")


def _basename(path: str) -> str:
    return PurePosixPath(path).name.lower()


def _suffix(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def path_tokens(path: str) -> tuple[str, ...]:
    return tuple(t for t in _TOKEN_SPLIT_RE.split(path.lower()) if t)


def is_security_sensitive_path(path: str) -> bool:
    return any(token in _SECURITY_TOKENS for token in path_tokens(path))


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path))


def classify_path(path: str) -> frozenset[FileChangeClass]:
    """All classes that apply to ``path``. A path with no special class
    is ``CODE``. ``DOCS``/``GENERATED``/``VENDOR``/``LOCKFILE`` are
    exclusive "non-code" classes and never combine with ``CODE``."""

    lowered = path.lower()
    base = _basename(path)
    suffix = _suffix(path)
    parts = PurePosixPath(lowered).parts

    if any(p in _VENDOR_DIRS for p in parts[:-1]):
        return frozenset({FileChangeClass.VENDOR})
    if base in _LOCKFILES:
        return frozenset({FileChangeClass.LOCKFILE})
    if any(lowered.endswith(s) for s in _GENERATED_SUFFIXES) or any(i in base for i in _GENERATED_INFIXES):
        return frozenset({FileChangeClass.GENERATED})
    if any(p in _GENERATED_DIRS for p in parts[:-1]):
        return frozenset({FileChangeClass.GENERATED})

    classes: set[FileChangeClass] = set()
    if base in _MANIFESTS or _REQUIREMENTS_RE.match(base):
        classes.add(FileChangeClass.MANIFEST)
    if any(p.search(lowered) for p in _CI_PATTERNS):
        classes.add(FileChangeClass.CI)
    if any(p.search(lowered) for p in _MIGRATION_DIR_PATTERNS):
        classes.add(FileChangeClass.MIGRATION)
    if suffix in _SCHEMA_EXTENSIONS or _SCHEMA_BASENAME_RE.match(base) or _MODEL_PATH_RE.search(lowered):
        classes.add(FileChangeClass.SCHEMA)
    if base in _CONFIG_BASENAMES or base.startswith("dockerfile") or (
        suffix in _CONFIG_EXTENSIONS and FileChangeClass.CI not in classes and FileChangeClass.MANIFEST not in classes
    ):
        classes.add(FileChangeClass.CONFIG)

    if not classes:
        is_doc = suffix in _DOC_EXTENSIONS or base in _DOC_BASENAMES or (
            bool(parts[:-1]) and parts[0] in _DOC_DIRS and suffix in _DOC_ASSET_EXTENSIONS
        )
        if is_doc:
            return frozenset({FileChangeClass.DOCS})

    if is_test_path(path) and not classes & {FileChangeClass.CI, FileChangeClass.MANIFEST}:
        classes.add(FileChangeClass.TEST)
    elif not classes or classes <= {FileChangeClass.SCHEMA, FileChangeClass.MIGRATION, FileChangeClass.CONFIG}:
        classes.add(FileChangeClass.CODE)
    return frozenset(classes)


NON_CODE_CLASSES = frozenset(
    {FileChangeClass.DOCS, FileChangeClass.GENERATED, FileChangeClass.VENDOR, FileChangeClass.LOCKFILE}
)


__all__ = [
    "NON_CODE_CLASSES",
    "classify_path",
    "is_security_sensitive_path",
    "is_test_path",
    "path_tokens",
]
