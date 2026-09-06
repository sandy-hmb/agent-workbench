from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from schema_validation import SchemaValidationError, validate
from workspace_input import load_workspace_input
from workspace_model import (
    BRANCH_NAME_PATTERN,
    REMOTE_PATTERN,
    WorkspaceError,
    _git_branch_valid,
    parse_workspace,
    valid_remote,
)

class SchemaValidationTest(unittest.TestCase):
    def test_types_and_unknown_properties(self):
        schema = {"type": "object", "required": ["x"], "additionalProperties": False, "properties": {"x": {"type": "integer"}}}
        validate({"x": 1}, schema)
        with self.assertRaises(SchemaValidationError): validate({"x": "1"}, schema)
        with self.assertRaises(SchemaValidationError): validate({"x": 1, "y": 2}, schema)
    def test_local_ref_and_unique_items(self):
        schema = {"$defs": {"s": {"type": "string"}}, "type": "array", "uniqueItems": True, "items": {"$ref": "#/$defs/s"}}
        validate(["a", "b"], schema)
        with self.assertRaises(SchemaValidationError): validate(["a", "a"], schema)

    def test_cyclic_local_refs_raise_schema_validation_error_not_recursion_error(self):
        schema = {
            "$defs": {
                "first": {"$ref": "#/$defs/second"},
                "second": {"$ref": "#/$defs/first"},
            },
            "$ref": "#/$defs/first",
        }
        with self.assertRaises(SchemaValidationError):
            validate({}, schema)

    def test_ref_sibling_constraints_are_applied(self):
        schema = {
            "$defs": {"name": {"type": "string"}},
            "$ref": "#/$defs/name",
            "const": "expected",
        }
        validate("expected", schema)
        with self.assertRaises(SchemaValidationError):
            validate("other", schema)

    def test_json_equality_does_not_treat_boolean_as_number(self):
        with self.assertRaises(SchemaValidationError):
            validate(True, {"const": 1})
        validate([True, 1, False, 0], {"type": "array", "uniqueItems": True})
        with self.assertRaises(SchemaValidationError):
            validate([True, True], {"type": "array", "uniqueItems": True})
        with self.assertRaises(SchemaValidationError):
            validate(True, {"enum": [1]})
    def test_unsupported_keyword_is_rejected(self):
        with self.assertRaises(SchemaValidationError): validate(1, {"type": "integer", "maximum": 2})

    def test_malformed_schema_keywords_raise_schema_validation_error(self):
        cases = (
            {"type": 1},
            {"type": []},
            {"type": ["string", 1]},
            {"properties": []},
            {"required": "field"},
            {"items": []},
            {"additionalProperties": "false"},
            {"propertyNames": []},
            {"pattern": []},
            {"minLength": "1"},
            {"minItems": -1},
            {"uniqueItems": 1},
            {"$defs": []},
        )
        for schema in cases:
            with self.subTest(schema=schema):
                with self.assertRaises(SchemaValidationError):
                    validate({}, schema)

    def test_standard_schema_annotation_metadata_is_ignored(self):
        validate(
            {"team": "platform"},
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "example-config",
                "title": "Example config",
                "description": "metadata only",
                "type": "object",
                "required": ["team"],
                "additionalProperties": False,
                "properties": {"team": {"type": "string", "description": "name"}},
            },
        )
    def test_checked_in_workspace_input_schema_is_self_contained(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "schemas/workspace-input.schema.json").read_text())
        schema.pop("$schema", None); schema.pop("$id", None)
        validate({"version": {"major": 1, "minor": 0}, "workspace": {"name": "Demo"}, "context": {}, "branchPolicy": {}, "extensions": {"providers": {}, "config": {}}, "repositories": []}, schema)
        validate(
            {
                "workspace": {"name": "Minimal"},
                "repositories": [{"path": "service", "remote": "https://example.test/service.git"}],
            },
            schema,
        )

    def test_term_router_schema_is_strict_without_restricting_other_context_fields(self):
        root = Path(__file__).resolve().parents[1]
        workspace = {
            "version": {"major": 1, "minor": 0},
            "workspace": {"name": "Demo"},
            "context": {
                "description": "neutral context remains allowed",
                "termRouter": {
                    "products": [{"term": "payments", "repository": "service"}],
                    "capabilities": [{"term": "refund", "capability": "refund"}],
                    "actions": [{"term": "review", "action": "review"}],
                    "repositories": [{"term": "svc", "repository": "service"}],
                },
            },
            "branchPolicy": {},
            "extensions": {"providers": {}, "config": {}},
            "repositories": [],
        }
        for name in ("workspace.schema.json", "workspace-input.schema.json"):
            schema = json.loads((root / "schemas" / name).read_text())
            with self.subTest(schema=name):
                validate(workspace, schema)
                invalid = json.loads(json.dumps(workspace))
                invalid["context"]["termRouter"]["products"][0]["extra"] = True
                with self.assertRaises(SchemaValidationError):
                    validate(invalid, schema)
                invalid = json.loads(json.dumps(workspace))
                del invalid["context"]["termRouter"]["actions"]
                with self.assertRaises(SchemaValidationError):
                    validate(invalid, schema)

    def test_workspace_schema_matches_remote_and_branch_policy_runtime_boundaries(self):
        kit_root = Path(__file__).resolve().parents[1]
        base = {
            "version": {"major": 1, "minor": 0},
            "workspace": {"name": "Demo"},
            "context": {},
            "branchPolicy": {
                "workBase": "develop",
                "testTarget": "test",
                "hotfixBase": "main",
                "namePattern": "{owner}/{type}/{slug}",
            },
            "extensions": {"providers": {}, "config": {}},
            "repositories": [
                {
                    "path": "service",
                    "aliases": [],
                    "remote": "https://example.com/team/service.git",
                    "category": "backend",
                    "description": "Service",
                    "instruction": "docs/repositories/service.md",
                }
            ],
        }
        accepted_remotes = (
            "git@example.com:team/repo.git",
            "ssh://git@example.com/team/repo.git",
            "ssh://git@example.com:22/team/repo.git",
            "https://example.com/team/repo.git",
            "https://xn--xample-9ua.example:65535/team/repo.git",
            "https://example.com:1/team/repo.git",
            None,
        )
        rejected_remotes = (
            "https://éxample.test/team/repo.git",
            "https://example.com/team/repo.git\n",
            "HTTPS://example.com/team/repo.git",
            "https://user@example.com/team/repo.git",
            "https://user:pass@example.com/team/repo.git",
            "ssh://git:pass@example.com/team/repo.git",
            "ssh://git@example.com:/team/repo.git",
            "https://example.com/team repo.git",
            "https://example.com:/team/repo.git",
            "https://example.com:0/team/repo.git",
            "https://example.com:65536/team/repo.git",
            "https://example.com:99999/team/repo.git",
            "https://example.com/team/repo.git?branch=main",
            "https://example.com/team/repo.git#main",
        )
        branch_cases = {
            "develop": True,
            "refs/feature": True,
            "release/2026.09": True,
            "foo..bar": False,
            "@{-1}": False,
            "develop branch": False,
            "-leading": False,
            "trailing.": False,
            "foo//bar": False,
            "foo/.bar": False,
            "foo/foo.lock": False,
            "foo/": False,
            "/foo": False,
        }

        def schema_accepts(candidate, schema):
            try:
                validate(candidate, schema)
            except SchemaValidationError:
                return False
            return True

        with tempfile.TemporaryDirectory() as directory:
            runtime_root = Path(directory) / "kit"
            runtime_root.mkdir()

            def runtime_accepts(candidate, *, input_schema):
                try:
                    if input_schema:
                        config = runtime_root / "input.json"
                        config.write_text(json.dumps(candidate), encoding="utf-8")
                        load_workspace_input(runtime_root, config, require_local=True)
                    else:
                        parse_workspace(candidate, runtime_root)
                except WorkspaceError:
                    return False
                return True

            for name in ("workspace.schema.json", "workspace-input.schema.json"):
                input_schema = name == "workspace-input.schema.json"
                schema = json.loads((kit_root / "schemas" / name).read_text())
                repository_schema = schema["$defs"]["repository"]["properties"]
                branch_schema = schema["$defs"]["branchPolicy"]["properties"]
                self.assertEqual(REMOTE_PATTERN, repository_schema["remote"]["pattern"])
                self.assertEqual(BRANCH_NAME_PATTERN, branch_schema["workBase"]["pattern"])
                for remote, expected in (
                    *((value, True) for value in accepted_remotes),
                    *((value, False) for value in rejected_remotes),
                ):
                    candidate = json.loads(json.dumps(base))
                    candidate["repositories"][0]["remote"] = remote
                    if input_schema:
                        candidate["local"] = {
                            "branchOwner": "alice",
                            "primaryRole": None,
                            "extensions": {},
                        }
                    with self.subTest(schema=name, remote=remote):
                        schema_result = schema_accepts(candidate, schema)
                        self.assertEqual(expected, schema_result)
                        self.assertEqual(expected, valid_remote(remote))
                        self.assertEqual(schema_result, runtime_accepts(candidate, input_schema=input_schema))
                for branch, expected in branch_cases.items():
                    candidate = json.loads(json.dumps(base))
                    candidate["branchPolicy"]["workBase"] = branch
                    if input_schema:
                        candidate["local"] = {
                            "branchOwner": "alice",
                            "primaryRole": None,
                            "extensions": {},
                        }
                    with self.subTest(schema=name, branch=branch):
                        schema_result = schema_accepts(candidate, schema)
                        self.assertEqual(expected, schema_result)
                        self.assertEqual(expected, _git_branch_valid(branch))
                        self.assertEqual(schema_result, runtime_accepts(candidate, input_schema=input_schema))

    def test_extension_schemas_accept_valid_and_reject_invalid_samples(self):
        root = Path(__file__).resolve().parents[1]
        digest = "sha256:" + "a" * 64
        cases = {
            "workspace-extension.schema.json": ({"schemaVersion":1,"id":"example-extension","version":"1.0.0","kitApi":1,"provides":[{"capability":"branch.naming","provider":"team","apiVersion":1,"skill":"branching","command":["python3","arg with space"]}],"actions":[],"requires":[],"effects":[]}, {"schemaVersion":1,"id":"example-extension","version":"1.0.0","kitApi":1,"provides":[{"capability":"branch.naming","provider":"team","apiVersion":1,"skill":"branching","command":["/bin/sh"]}],"actions":[],"requires":[],"effects":[]}),
            "extensions-lock.schema.json": ({"lockVersion":{"major":1,"minor":0},"kitApi":1,"extensions":[{"id":"e","version":"1.0.0","path":"extensions/e","digest":digest,"providers":[],"actions":[],"adapters":[{"agentPath":".agents/skills/local-e-s","claudePath":".claude/skills/local-e-s","skill":"s","installedDigest":digest}]}],"providers":{}}, {"lockVersion":{"major":1,"minor":0},"kitApi":1,"extensions":[{"id":"e","version":"1.0.0","path":"extensions/e","digest":digest,"providers":[]}],"providers":{}}),
            "provider-result.schema.json": ({"apiVersion":1,"provider":"e/p","status":"ok","result":{},"diagnostics":[{"level":"info","code":"READY","message":"ready"}],"effects":[]}, {"apiVersion":1,"provider":"e/p","status":"ok","result":{},"diagnostics":[{"level":"debug","code":"bad-code","message":""}],"effects":[]}),
        }
        for name, (valid, invalid) in cases.items():
            schema = json.loads((root / "schemas" / name).read_text())
            with self.subTest(schema=name):
                validate(valid, schema)
                with self.assertRaises(SchemaValidationError): validate(invalid, schema)
        workflow_schema = json.loads((root / "schemas/workspace-workflow.schema.json").read_text())
        workflow = {
            "schemaVersion": {"major": 1, "minor": 0},
            "workflow": "feature-development",
            "stages": [
                {
                    "id": "team-check.integration-test",
                    "after": "feature.implement",
                    "uses": "team-check/integration-test",
                }
            ],
        }
        validate(workflow, workflow_schema)
        invalid_workflow = json.loads(json.dumps(workflow))
        invalid_workflow["stages"][0]["uses"] = "invalid"
        with self.assertRaises(SchemaValidationError):
            validate(invalid_workflow, workflow_schema)
        manifest_schema = json.loads((root / "schemas/workspace-extension.schema.json").read_text())
        with_config = {"schemaVersion":1,"id":"e","version":"1.0.0","kitApi":1,"provides":[],"actions":[],"requires":[],"configSchema":"./schemas/config.json","effects":[]}
        validate(with_config, manifest_schema)
        for command in (
            ["curl", "--config=/etc/passwd"],
            ["curl", "--file=../../outside"],
            ["git", "-C/etc"],
            ["git", "-C", "/etc"],
            ["curl", "--input=file:///etc/passwd"],
        ):
            invalid_command = {"schemaVersion": {"major": 1, "minor": 0},"id":"e","version":"1.0.0","kitApi": 1,"provides":[{"capability":"branch.naming","provider":"p","apiVersion":1,"skill":"s","command":command}],"requires":[],"effects":[]}
            with self.subTest(command=command), self.assertRaises(SchemaValidationError):
                validate(invalid_command, manifest_schema)
        for field, value in (("configSchema", "schemas/config\0.json"), ("command", ["python3\0bad"])):
            manifest = {"schemaVersion": {"major": 1, "minor": 0},"id":"e","version":"1.0.0","kitApi": 1,"provides":[],"requires":[],"effects":[]}
            if field == "configSchema":
                manifest[field] = value
            else:
                manifest["provides"] = [{"capability":"branch.naming","provider":"p","apiVersion":1,"skill":"s","command":value}]
            with self.subTest(nul=field), self.assertRaises(SchemaValidationError): validate(manifest, manifest_schema)
        lock_schema = json.loads((root / "schemas/extensions-lock.schema.json").read_text())
        bad_adapter_lock = cases["extensions-lock.schema.json"][0]
        bad_adapter_lock["extensions"][0]["adapters"][0]["agentPath"] = ".agents/skills/bad\0path"
        with self.assertRaises(SchemaValidationError): validate(bad_adapter_lock, lock_schema)
        action_manifest = {
            "schemaVersion": 1,
            "id": "e",
            "version": "1.0.0",
            "kitApi": 1,
            "provides": [],
            "actions": [
                {
                    "id": "run",
                    "apiVersion": 1,
                    "skill": "run",
                    "effects": [],
                    "confirmation": {"title": "Run", "summary": "Run the action."},
                }
            ],
            "requires": [],
            "effects": [],
        }
        validate(action_manifest, manifest_schema)
        missing_confirmation = json.loads(json.dumps(action_manifest))
        del missing_confirmation["actions"][0]["confirmation"]
        with self.assertRaises(SchemaValidationError):
            validate(missing_confirmation, manifest_schema)
        future_action = json.loads(json.dumps(action_manifest))
        future_action["actions"][0]["apiVersion"] = 2
        with self.assertRaises(SchemaValidationError):
            validate(future_action, manifest_schema)

if __name__ == "__main__": unittest.main()
