"""M5.2/M5.9: manifest/lockfile version extraction and source scanners."""

from __future__ import annotations

import json

import pytest

from patchfrog.dependencies.domain import Ecosystem
from patchfrog.dependencies.files import is_secret_store_path
from patchfrog.dependencies.manifests import parse_lockfile, parse_manifest, sanitize_spec
from patchfrog.dependencies.scan import (
    ObservationKind,
    javascript_symbol_spans,
    sanitized_url_path,
    scan_javascript,
    scan_python,
)

# -- versions ------------------------------------------------------------------------


def test_requirements_pyproject_and_package_json_declarations() -> None:
    reqs = parse_manifest("requirements.txt", "OpenAI==1.40.0\n# comment\n-r base.txt\nrequests>=2.31 ; python_version>'3'\n")
    assert [(d.name, d.spec) for d in reqs] == [("openai", "==1.40.0"), ("requests", ">=2.31")]
    pyproject = parse_manifest(
        "pyproject.toml",
        '[project]\ndependencies = ["stripe>=9,<10", "PyGithub[extra]==2.3"]\n'
        '[tool.poetry.dependencies]\npython = "^3.12"\nopenai = {version = "^1.30"}\n',
    )
    assert {(d.name, d.spec) for d in pyproject} == {("stripe", ">=9,<10"), ("pygithub", "==2.3"), ("openai", "^1.30")}
    npm = parse_manifest("web/package.json", json.dumps({"dependencies": {"stripe": "^12.0.0"}, "devDependencies": {"openai": "4.1.0"}}))
    assert {(d.ecosystem, d.name, d.spec) for d in npm} == {(Ecosystem.NPM, "stripe", "^12.0.0"), (Ecosystem.NPM, "openai", "4.1.0")}
    gomod = parse_manifest("go.mod", "module x\nrequire (\n\tgithub.com/stripe/stripe-go v76.2.0\n)\n")
    assert [(d.name, d.spec) for d in gomod] == [("github.com/stripe/stripe-go", "v76.2.0")]


def test_lockfile_resolutions() -> None:
    npm = parse_lockfile(
        "package-lock.json",
        json.dumps({"lockfileVersion": 3, "packages": {"": {}, "node_modules/stripe": {"version": "12.3.0"}}}),
    )
    assert [(r.name, r.version) for r in npm] == [("stripe", "12.3.0")]
    yarn = parse_lockfile("yarn.lock", 'stripe@^12.0.0:\n  version "12.4.1"\n  resolved "x"\n')
    assert [(r.name, r.version) for r in yarn] == [("stripe", "12.4.1")]
    poetry = parse_lockfile("poetry.lock", '[[package]]\nname = "OpenAI"\nversion = "1.40.0"\n')
    assert [(r.name, r.version) for r in poetry] == [("openai", "1.40.0")]
    pnpm = parse_lockfile("pnpm-lock.yaml", "packages:\n  /stripe@12.5.0:\n    resolution: {}\n")
    assert [(r.name, r.version) for r in pnpm] == [("stripe", "12.5.0")]


def test_malformed_manifests_yield_nothing_instead_of_raising() -> None:
    assert parse_manifest("package.json", "{not json") == []
    assert parse_lockfile("poetry.lock", "[[[") == []


@pytest.mark.parametrize(
    "spec",
    ["git+https://user:not-a-real-token@github.com/acme/sdk.git#v1", "https://example.com/pkg.tgz?token=xyz"],
)
def test_url_specs_never_retain_credentials_paths_or_queries(spec: str) -> None:
    cleaned = sanitize_spec(spec)
    assert cleaned is not None and cleaned.startswith("url")
    assert "not-a-real-token" not in cleaned and "token" not in cleaned and "acme" not in cleaned


# -- secret stores ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", [".env", ".env.production", "config/.env.local", "id_rsa", "deploy/key.pem", "secrets.yaml", ".npmrc"]
)
def test_secret_store_files_are_recognized_by_name(path: str) -> None:
    assert is_secret_store_path(path)


@pytest.mark.parametrize("path", ["app/tokenizer.py", "package.json", "src/secretary.py", "docs/env.md"])
def test_ordinary_files_are_not_secret_stores(path: str) -> None:
    assert not is_secret_store_path(path)


# -- python scanner --------------------------------------------------------------------------


def test_python_env_var_names_are_captured_but_never_their_values() -> None:
    source = (
        "import os\n"
        'A = os.environ["OPENAI_API_KEY"]\n'
        'B = os.getenv("STRIPE_SECRET_KEY", "PFTEST-FAKE-DEFAULT-VALUE-123")\n'
        'C = os.environ.get("GITHUB_TOKEN", "PFTEST-FAKE-DEFAULT-VALUE-456")\n'
        'D = os.environ.get("lowercase_name")\n'
    )
    observations = scan_python(source, tracked_modules=frozenset())
    names = {o.subject for o in observations if o.kind is ObservationKind.ENV_VAR}
    assert names == {"OPENAI_API_KEY", "STRIPE_SECRET_KEY", "GITHUB_TOKEN"}
    rendered = repr(observations)
    assert "PFTEST-FAKE-DEFAULT-VALUE" not in rendered


def test_python_sdk_import_constructor_and_call_chains() -> None:
    source = (
        "from openai import OpenAI as Client\n"
        "import stripe\n"
        "c = Client()\n"
        "def f():\n"
        "    return c.embeddings.create(input='x')\n"
        "def g():\n"
        "    return stripe.Customer.create(email='e')\n"
    )
    observations = scan_python(source, tracked_modules=frozenset({"openai", "stripe"}))
    calls = {(o.subject, o.detail) for o in observations if o.kind is ObservationKind.CALL}
    assert calls == {("openai", "embeddings.create"), ("stripe", "Customer.create")}
    assert any(o.kind is ObservationKind.CONSTRUCTOR and o.detail == "Client" for o in observations)


def test_urls_keep_host_and_sanitized_path_only() -> None:
    assert sanitized_url_path("https://api.github.com/repos/a/b?access_token=zzz#frag") == "/repos/a/b"
    assert sanitized_url_path("https://hooks.example.com/T0A1B2C3D4E5F6G7H8I9J0K") == "/{redacted}"
    source = 'import requests\nrequests.get(f"https://api.github.com/repos/{o}/{r}?token={t}")\n'
    urls = [o for o in scan_python(source, tracked_modules=frozenset()) if o.kind is ObservationKind.URL]
    assert [(u.subject, u.detail) for u in urls] == [("api.github.com", "/repos/{}/{}")]


def test_python_syntax_error_yields_nothing() -> None:
    assert scan_python("def broken(:\n", tracked_modules=frozenset({"openai"})) == []


# -- javascript scanner ------------------------------------------------------------------------


def test_js_comments_never_create_imports_and_urls_in_strings_survive() -> None:
    source = (
        '// import Stripe from "stripe";\n'
        '/* const OpenAI = require("openai"); */\n'
        'const url = "https://api.stripe.com/v1/charges"; // trailing\n'
    )
    observations = scan_javascript(source, tracked_modules=frozenset({"stripe", "openai"}))
    assert not [o for o in observations if o.kind is ObservationKind.IMPORT]
    assert [(o.subject, o.detail) for o in observations if o.kind is ObservationKind.URL] == [
        ("api.stripe.com", "/v1/charges")
    ]


def test_js_require_import_and_instance_calls() -> None:
    source = (
        "const Stripe = require('stripe');\n"
        "const stripe = Stripe(process.env.STRIPE_SECRET_KEY);\n"
        "import { Octokit } from '@octokit/rest';\n"
        "const gh = new Octokit({ auth: token });\n"
        "async function comment() {\n"
        "  await gh.rest.issues.createComment({ owner, repo });\n"
        "}\n"
    )
    observations = scan_javascript(source, tracked_modules=frozenset({"stripe", "@octokit/rest"}))
    calls = {(o.subject, o.detail) for o in observations if o.kind is ObservationKind.CALL}
    assert ("@octokit/rest", "rest.issues.createComment") in calls
    assert {o.subject for o in observations if o.kind is ObservationKind.ENV_VAR} == {"STRIPE_SECRET_KEY"}
    assert ("comment", 5, 7) in javascript_symbol_spans(source)
