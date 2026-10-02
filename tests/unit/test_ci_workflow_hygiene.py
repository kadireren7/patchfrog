"""M11.13: CI must not render secret material into job logs.

History: a disposable RSA key used to be written into the workflow's job
``env:`` block. GitHub prints a step's resolved ``env:`` in the step header, so
the full key landed in plain-text logs. This test guards the *configuration*
against reintroducing that. It cannot (and does not claim to) clean logs that
were already written.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).resolve().parents[2] / ".github" / "workflows").glob("*.y*ml"))
SECRETISH = re.compile(r"(PRIVATE_KEY|SECRET|TOKEN|PASSWORD|API_KEY|CREDENTIAL)", re.IGNORECASE)
EXPRESSION = re.compile(r"^\$\{\{\s*(secrets|vars)\.[A-Za-z0-9_]+\s*\}\}$")
#: The only secret-looking literal allowed: the throw-away password of the Postgres *service
#: container* (it protects an empty database that exists for the length of one job).
ALLOWED_LITERALS = {"POSTGRES_PASSWORD"}


def _env_blocks(node: Any, path: str = "") -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if key == "env" and isinstance(value, dict):
                found.append((here, value))
            found.extend(_env_blocks(value, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_env_blocks(value, f"{path}[{index}]"))
    return found


def test_there_is_at_least_one_workflow() -> None:
    assert WORKFLOWS, "no workflows found -- the hygiene check would be vacuous"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_no_secret_looking_literal_in_any_env_block(workflow: Path) -> None:
    document = yaml.safe_load(workflow.read_text())
    for where, env in _env_blocks(document):
        for name, value in env.items():
            if not SECRETISH.search(str(name)) or str(name) in ALLOWED_LITERALS:
                continue
            assert EXPRESSION.match(str(value).strip()), (
                f"{workflow.name}: {where}.{name} holds a literal; secret-looking values must come from "
                "`${{ secrets.* }}` or be generated and masked at run time (the env block is printed in step logs)"
            )


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_no_key_material_or_long_encoded_blob_in_the_workflow_text(workflow: Path) -> None:
    text = workflow.read_text()
    assert not re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text)
    assert not re.search(r"\bMII[A-Za-z0-9+/]{40,}", text), "a PEM/DER body appears to be embedded"
    assert not re.search(r"[A-Za-z0-9+/]{80,}={0,2}", text), "a long encoded blob appears to be embedded"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_no_command_that_prints_the_environment_or_resolved_config(workflow: Path) -> None:
    for line in workflow.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not re.search(r"\bset -[a-z]*x", stripped), f"shell tracing prints expanded secrets: {stripped!r}"
        assert not re.search(r"\b(printenv|env\s*\||export -p)\b", stripped), f"prints the environment: {stripped!r}"
        assert "docker compose config" not in stripped and "docker-compose config" not in stripped
        assert not re.search(r"cat\s+\S*\.env\b", stripped)


def test_the_ci_job_generates_its_disposable_credentials_at_run_time() -> None:
    document = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").read_text())
    scripts = [
        str(step.get("run", ""))
        for job in document["jobs"].values()
        for step in job.get("steps", [])
    ]
    generator = next((s for s in scripts if "openssl genrsa" in s), None)
    assert generator is not None, "no step generates the disposable key"
    assert "::add-mask::" in generator and "GITHUB_ENV" in generator and "GITHUB_PRIVATE_KEY_PATH" in generator
    # the container check mounts the key file; it never passes the key's content as an env value
    assert all(not re.search(r"-e\s+GITHUB_PRIVATE_KEY=", s) for s in scripts)
