"""Exercise the actual GitHub release shell helper using a fake gh executable."""

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / ".github/actions/ecr-release/github_release.sh"
SHA = "1ce448c" + "a" * 33
ORDERED = "release-2026.09.24.153012"
pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="The GitHub release helper runs on Linux runners"
)


@pytest.fixture
def github(tmp_path):
    executable = tmp_path / "gh"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["GH_CALLS"]).open("a") as log:
    log.write(json.dumps(args) + "\\n")
scenario = os.environ.get("SCENARIO", "fresh")
sha = os.environ["TEST_SHA"]
if args[0] == "api":
    if "POST" in args:
        sys.exit(0)
    endpoint = args[1]
    if "/commits/" in endpoint:
        if scenario == "commit-missing":
            sys.exit(1)
        print(sha)
    elif "/git/matching-refs/" in endpoint:
        if scenario == "tag-api-failure":
            sys.exit(1)
        version = endpoint.endswith("release-v0.1.16")
        if (scenario == "version-conflict" and version) or (scenario == "ordered-conflict" and not version):
            print("commit\\t" + "c" * 40)
        elif scenario == "annotated" and version:
            print("tag\\t" + "b" * 40)
        elif scenario == "release-exists":
            print("commit\\t" + sha)
    elif "/git/tags/" in endpoint:
        print("commit\\t" + sha)
    else:
        raise RuntimeError(args)
elif args[:2] == ["release", "list"]:
    if scenario == "history-failure":
        sys.exit(1)
    print("release-2026.09.20.120000\\nrelease-2026.09.23.120000\\nrelease-2026.09.25.120000")
elif args[:2] == ["release", "view"]:
    sys.exit(0 if scenario == "release-exists" else 1)
elif args[:2] != ["release", "create"]:
    raise RuntimeError(args)
"""
    )
    executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "GH_TOKEN": "testing",
        "GH_REPO": "CitrineInformatics/scaler",
        "SHORT_SHA": "1ce448c",
        "SHA": SHA,
        "RELEASE_TAG": "release-v0.1.16",
        "ORDERED_TAG": ORDERED,
        "TITLE": "Release title",
        "NOTES_FILE": str(tmp_path / "notes.md"),
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "GH_CALLS": str(tmp_path / "calls.jsonl"),
        "TEST_SHA": SHA,
    }

    def invoke(phase, scenario="fresh", **overrides):
        result = subprocess.run(
            ["bash", str(SCRIPT), phase],
            env={**env, "SCENARIO": scenario, **overrides},
            text=True,
            capture_output=True,
        )
        calls_path = tmp_path / "calls.jsonl"
        calls = (
            [json.loads(line) for line in calls_path.read_text().splitlines()]
            if calls_path.exists()
            else []
        )
        writes = [
            call
            for call in calls
            if "POST" in call or call[:2] == ["release", "create"]
        ]
        return result, calls, writes

    return invoke, tmp_path


@pytest.mark.parametrize(
    "scenario",
    [
        "commit-missing",
        "version-conflict",
        "ordered-conflict",
        "tag-api-failure",
        "history-failure",
    ],
)
def test_failed_preflight_performs_zero_writes(github, scenario):
    invoke, tmp_path = github
    result, _, writes = invoke("preflight", scenario)
    assert result.returncode != 0
    assert writes == []
    assert not (tmp_path / "outputs").exists()


@pytest.mark.parametrize("scenario", ["fresh", "annotated", "release-exists"])
def test_successful_preflight_only_reads_and_outputs_exact_commit(github, scenario):
    invoke, tmp_path = github
    result, _, writes = invoke("preflight", scenario)
    assert result.returncode == 0, result.stderr
    assert writes == []
    assert (tmp_path / "outputs").read_text() == f"sha={SHA}\n"


@pytest.mark.parametrize(
    "scenario",
    ["version-conflict", "ordered-conflict", "tag-api-failure", "history-failure"],
)
def test_record_rechecks_before_git_writes(github, scenario):
    invoke, _ = github
    result, _, writes = invoke("record", scenario)
    assert result.returncode != 0
    assert writes == []


def test_records_release_on_preflight_commit_with_previous_release_notes(github):
    invoke, _ = github
    result, calls, writes = invoke("record")
    assert result.returncode == 0, result.stderr
    assert len(writes) == 2
    assert [call for call in calls if "/commits/" in " ".join(call)] == []
    assert f"sha={SHA}" in writes[0]
    release_call = writes[1]
    assert release_call[:3] == ["release", "create", ORDERED]
    assert release_call[release_call.index("--target") + 1] == SHA
    assert (
        release_call[release_call.index("--notes-start-tag") + 1]
        == "release-2026.09.23.120000"
    )


def test_record_accepts_existing_annotated_version_tag(github):
    invoke, _ = github
    result, _, writes = invoke("record", "annotated")
    assert result.returncode == 0, result.stderr
    assert len(writes) == 1
    assert writes[0][:2] == ["release", "create"]


def test_existing_release_is_a_no_op(github):
    invoke, _ = github
    result, _, writes = invoke("record", "release-exists")
    assert result.returncode == 0, result.stderr
    assert writes == []


def test_release_without_version_only_creates_ordered_release(github):
    invoke, _ = github
    result, _, writes = invoke("record", RELEASE_TAG="")
    assert result.returncode == 0, result.stderr
    assert len(writes) == 1
    assert writes[0][:2] == ["release", "create"]


def test_record_requires_preflight_commit(github):
    invoke, _ = github
    result, calls, writes = invoke("record", SHA="")
    assert result.returncode != 0
    assert calls == writes == []


def test_unknown_phase_is_rejected_without_api_calls(github):
    invoke, _ = github
    result, calls, writes = invoke("invalid")
    assert result.returncode != 0
    assert calls == writes == []
