#!/usr/bin/env python3
"""Offline structural and fixture validation of the Harniqo M0 contract.

Default validation never modifies the source. --write-openapi only regenerates
the self-contained OpenAPI artifact and uses the Python standard library.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from uuid import UUID


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "packages" / "contracts"
DIALECT = "https://json-schema.org/draft/2020-12/schema"


def read_json(path: Path):
    def no_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"Duplicate JSON property: {key}")
            value[key] = item
        return value

    return json.loads(path.read_text(), object_pairs_hook=no_duplicates)


def canonical_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def walk(value, path=()):
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from walk(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, (*path, index))


def pointer(document, ref):
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise ValueError(f"Only in-document JSON Pointer refs are allowed: {ref!r}")
    current = document
    for encoded in ref[2:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current


def check_refs(document):
    count = 0
    for path, value in walk(document):
        if "$dynamicRef" in value or "$recursiveRef" in value:
            raise ValueError(f"Dynamic/recursive refs are not permitted: {path}")
        if "$ref" in value:
            pointer(document, value["$ref"])
            count += 1
    return count


def build_openapi(schema, routes):
    result = copy.deepcopy(routes)
    result.setdefault("components", {})["schemas"] = copy.deepcopy(schema["$defs"])
    for _, value in walk(result):
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            value["$ref"] = "#/components/schemas/" + ref[len("#/$defs/"):]
    check_refs(result)
    return result


def errors_recursive(errors):
    for error in errors:
        yield error
        yield from errors_recursive(error.context)


def format_checker():
    from jsonschema import FormatChecker

    checker = FormatChecker()

    @checker.checks("date-time", raises=(ValueError, TypeError))
    def utc_timestamp(value):
        if not isinstance(value, str):
            return True
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value):
            return False
        datetime.fromisoformat(value[:-1] + "+00:00")
        return True

    @checker.checks("uuid", raises=(ValueError, TypeError, AttributeError))
    def uuid_value(value):
        return not isinstance(value, str) or str(UUID(value)) == value

    @checker.checks("uri", raises=(ValueError, TypeError))
    def absolute_uri(value):
        if not isinstance(value, str):
            return True
        parts = urlsplit(value)
        # Accessing port validates its syntax; credentials do not belong in a
        # public discovery URL or a logged contract field.
        _ = parts.port
        return (bool(parts.scheme and parts.netloc and parts.hostname)
                and parts.username is None and parts.password is None) and not any(
            char.isspace() for char in value
        )

    return checker


def validate_structure(schema, routes, openapi):
    from jsonschema import Draft202012Validator
    from openapi_spec_validator import validate

    if schema.get("$schema") != DIALECT:
        raise ValueError("Canonical contract must use draft2020-12")
    Draft202012Validator.check_schema(schema)
    for name in schema["$defs"]:
        Draft202012Validator.check_schema({
            "$schema": DIALECT,
            "$defs": schema["$defs"],
            "$ref": f"#/$defs/{name}",
        })
    check_refs(schema)
    check_refs(openapi)
    validate(openapi)
    if openapi != build_openapi(schema, routes):
        raise ValueError("OpenAPI artifact differs from canonical definitions/routes")

    # Requests and responses are closed for the explicitly negotiated draft.
    # Extensible native events carry bounded object references, not arbitrary
    # JSON introduced into the platform state machine.
    for path, value in walk(schema):
        if value.get("type") == "object" and value.get("additionalProperties") is not False:
            raise ValueError(f"Object schema lacks closed properties: {path}")
    operation_ids = []
    route_count = 0
    for path, path_item in routes["paths"].items():
        route_count += 1
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            if "operationId" not in operation or "responses" not in operation:
                raise ValueError(f"Incomplete operation: {method} {path}")
            if "security" not in operation:
                raise ValueError(f"Operation has no explicit security policy: {method} {path}")
            operation_ids.append(operation["operationId"])
    if len(operation_ids) != len(set(operation_ids)):
        raise ValueError("Duplicate OpenAPI operationId")
    return {"definitions": len(schema["$defs"]), "paths": route_count,
            "operations": len(operation_ids), "schema_refs": check_refs(schema),
            "openapi_refs": check_refs(openapi)}


def validate_fixtures(schema, fixtures):
    from jsonschema import Draft202012Validator

    if fixtures.get("synthetic_only") is not True:
        raise ValueError("Fixtures must be explicitly marked synthetic")
    checker = format_checker()
    validators = {}
    case_ids = set()
    valid = invalid = 0
    for case in fixtures["cases"]:
        if set(case) - {"id", "schema", "valid", "instance", "expected_keyword"}:
            raise ValueError(f"Unexpected fixture fields: {case['id']}")
        if case["id"] in case_ids:
            raise ValueError(f"Duplicate fixture id: {case['id']}")
        case_ids.add(case["id"])
        name = case["schema"]
        if name not in schema["$defs"] or not isinstance(case["valid"], bool):
            raise ValueError(f"Invalid fixture declaration: {case['id']}")
        validator = validators.setdefault(name, Draft202012Validator({
            "$schema": DIALECT,
            "$defs": schema["$defs"],
            "$ref": f"#/$defs/{name}",
        }, format_checker=checker))
        errors = list(validator.iter_errors(case["instance"]))
        if case["valid"]:
            valid += 1
            if errors:
                first = errors[0]
                raise ValueError(f"Positive fixture {case['id']} rejected at "
                                 f"{list(first.absolute_path)}: {first.message}")
        else:
            invalid += 1
            if not errors:
                raise ValueError(f"Negative fixture {case['id']} was accepted")
            expected = case.get("expected_keyword")
            if not expected or expected not in {e.validator for e in errors_recursive(errors)}:
                raise ValueError(f"Negative fixture {case['id']} failed for unexpected reason; "
                                 f"expected keyword {expected!r}")
    if not valid or not invalid:
        raise ValueError("Both positive and negative fixture suites are required")
    return {"fixture_count": len(case_ids), "positive_fixtures": valid,
            "negative_fixtures": invalid}


def verify_ref_guard():
    rejected = 0
    for ref in ["https://example.invalid/schema.json", "file:///etc/passwd",
                "../outside.json#/$defs/Secret", "//example.invalid/schema.json"]:
        try:
            check_refs({"$ref": ref})
        except ValueError:
            rejected += 1
        else:
            raise ValueError(f"Unsafe ref was allowed: {ref}")
    try:
        check_refs({"$dynamicRef": "#anchor"})
    except ValueError:
        rejected += 1
    else:
        raise ValueError("Dynamic ref was allowed")
    return rejected


def validate_lifecycle(schema, lifecycle):
    states = set(schema["$defs"]["RunState"]["enum"])
    graph = lifecycle["transitions"]
    terminal = set(lifecycle["terminal_states"])
    if set(graph) != states or not terminal.issubset(states):
        raise ValueError("Lifecycle states differ from canonical RunState")
    for source, targets in graph.items():
        if len(targets) != len(set(targets)) or not set(targets).issubset(states):
            raise ValueError(f"Invalid transition targets for {source}")
        if source in terminal and targets:
            raise ValueError(f"Terminal state has outgoing transitions: {source}")
    reachable = {lifecycle["initial_state"]}
    while True:
        expanded = reachable | {target for state in reachable for target in graph[state]}
        if expanded == reachable:
            break
        reachable = expanded
    if reachable != states:
        raise ValueError("Lifecycle contains unreachable states")
    for case in lifecycle["transition_cases"]:
        allowed = case["to"] in graph.get(case["from"], [])
        if allowed is not case["valid"]:
            raise ValueError(f"Unexpected transition result: {case}")
    return {"lifecycle_states": len(states),
            "transition_cases": len(lifecycle["transition_cases"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-openapi", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    schema = read_json(CONTRACTS / "schemas/contract.schema.json")
    routes = read_json(CONTRACTS / "openapi.routes.json")
    check_refs(schema)
    expected = build_openapi(schema, routes)
    if args.write_openapi:
        (CONTRACTS / "openapi.json").write_bytes(canonical_bytes(expected))
        print("Generated self-contained OpenAPI; validation has not run.")
        return 0
    try:
        import jsonschema  # noqa: F401
        import openapi_spec_validator  # noqa: F401
    except ImportError as exc:
        print(f"Missing contract validator dependency: {exc.name}. Install "
              "scripts/contracts/requirements.txt in the project validation venv.", file=sys.stderr)
        return 2
    openapi = read_json(CONTRACTS / "openapi.json")
    if (CONTRACTS / "openapi.json").read_bytes() != canonical_bytes(expected):
        raise ValueError("Generated OpenAPI is stale; use --write-openapi")
    result = {"status": "passed", "contract_version": "0.1.0",
              "live_harness_verified": False, "live_vm_verified": False,
              "validators": {name: importlib.metadata.version(name)
                             for name in ["jsonschema", "openapi-spec-validator"]}}
    for line in (ROOT / "scripts/contracts/requirements.txt").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, expected_version = line.strip().split("==", 1)
        if result["validators"].get(name) != expected_version:
            raise ValueError(f"Validator version mismatch for {name}; install pinned requirements")
    result.update(validate_structure(schema, routes, openapi))
    result.update(validate_fixtures(schema, read_json(CONTRACTS / "fixtures/cases.json")))
    result.update(validate_lifecycle(schema, read_json(CONTRACTS / "run-lifecycle.json")))
    result["unsafe_ref_cases_rejected"] = verify_ref_guard()
    result["source_sha256"] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in sorted(CONTRACTS.rglob("*.json"))}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_bytes(canonical_bytes(result))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, IndexError) as exc:
        print(f"Contract validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
