"""Tests for .github/actions/ecr-release/ecr_release.py"""

import json
from datetime import datetime, timedelta, timezone

import boto3
import ecr_release  # registered on sys.path via pythonpath in pyproject.toml
import pytest
from botocore.stub import Stubber
from ecr_release import (
    Image,
    Release,
    ReleaseError,
    check_same_build,
    choose_ordered_tag,
    describe_by_tag,
    digest_for_tag,
    fetch_manifest,
    image_ref,
    is_ordered_tag,
    is_version_tag,
    main,
    ordered_tag_for,
    parse_list,
    release_title,
    render_notes,
    retag_everywhere,
    run,
    short_sha_from_tags,
    should_put,
    version_from_tags,
    write_outputs,
)

REPO = "platform/backend/scaler"
ACCOUNT = "123456789012"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
MANIFEST = '{"schemaVersion": 2}'
NOW = datetime(2026, 9, 24, 15, 30, 12, tzinfo=timezone.utc)
ORDERED = "release-2026.09.24.153012"
PUBLISHED_TAGS = ["main", "main-1ce448c", "v0.1.16"]


def make_release(release_tag="release-v0.1.16", regions=("us-east-1",)):
    return Release(
        source_tag="main",
        short_sha="1ce448c",
        release_tag=release_tag,
        ordered_tag=ORDERED,
        regions=list(regions),
        images=[Image(REPO, DIGEST, ACCOUNT)],
    )


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def stubbed():
    """Two regional ECR clients, each with an active Stubber, keyed by region."""
    clients = {
        r: boto3.client("ecr", region_name=r) for r in ("us-east-1", "eu-central-1")
    }
    stubbers = {r: Stubber(c) for r, c in clients.items()}
    for s in stubbers.values():
        s.activate()
    yield clients, stubbers
    for s in stubbers.values():
        s.assert_no_pending_responses()
        s.deactivate()


# --- Stub helpers ------------------------------------------------------------


def stub_describe(stubber, tag, digest=DIGEST, tags=None):
    stubber.add_response(
        "describe_images",
        {
            "imageDetails": [
                {
                    "registryId": ACCOUNT,
                    "repositoryName": REPO,
                    "imageDigest": digest,
                    "imageTags": tags if tags is not None else [tag],
                }
            ]
        },
        {"repositoryName": REPO, "imageIds": [{"imageTag": tag}]},
    )


def stub_not_found(stubber, tag):
    stubber.add_client_error(
        "describe_images",
        service_error_code="ImageNotFoundException",
        expected_params={"repositoryName": REPO, "imageIds": [{"imageTag": tag}]},
    )


def stub_manifest(stubber):
    stubber.add_response(
        "batch_get_image",
        {
            "images": [
                {
                    "imageId": {"imageDigest": DIGEST},
                    "imageManifest": MANIFEST,
                    "imageManifestMediaType": MEDIA_TYPE,
                }
            ],
            "failures": [],
        },
        {
            "repositoryName": REPO,
            "imageIds": [{"imageDigest": DIGEST}],
            "acceptedMediaTypes": ecr_release.MANIFEST_MEDIA_TYPES,
        },
    )


def stub_put(stubber, tag):
    stubber.add_response(
        "put_image",
        {},
        {
            "repositoryName": REPO,
            "imageManifest": MANIFEST,
            "imageManifestMediaType": MEDIA_TYPE,
            "imageTag": tag,
            "imageDigest": DIGEST,
        },
    )


# --- Pure helpers ------------------------------------------------------------


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("v0.1.16", True),
        ("v10.0.3", True),
        ("0.1.16", False),
        ("v0.1", False),
        ("v0.1.16-rc1", False),
        ("va.b.c", False),
        ("main-abc", False),
        ("", False),
    ],
)
def test_is_version_tag(tag, expected):
    assert is_version_tag(tag) is expected


@pytest.mark.parametrize(
    "tag, expected",
    [
        (ORDERED, True),
        ("release-v0.1.16", False),
        ("release-2026.13.01.000000", False),
        ("release-2026.09.24", False),
        ("main", False),
    ],
)
def test_is_ordered_tag(tag, expected):
    assert is_ordered_tag(tag) is expected


def test_ordered_tag_for_utc():
    assert ordered_tag_for(NOW) == ORDERED


def test_ordered_tag_for_converts_to_utc():
    eastern = NOW.astimezone(timezone(timedelta(hours=-4)))
    assert ordered_tag_for(eastern) == ORDERED


class TestChooseOrderedTag:
    def test_mints_from_now_when_none_exist(self):
        assert choose_ordered_tag(PUBLISHED_TAGS, NOW) == ORDERED

    def test_reuses_the_earliest_existing(self):
        existing = ["release-2026.09.20.000000", "release-2026.09.01.101010"]
        assert choose_ordered_tag(existing, NOW) == "release-2026.09.01.101010"

    def test_ignores_other_release_tags(self):
        assert choose_ordered_tag(["release-v0.1.16", "v0.1.16"], NOW) == ORDERED


class TestVersionFromTags:
    def test_found(self):
        assert version_from_tags(PUBLISHED_TAGS, "") == "v0.1.16"

    def test_missing(self):
        assert version_from_tags(["main", "main-1ce448c"], "") is None

    def test_override_wins(self):
        assert version_from_tags(PUBLISHED_TAGS, "v1.2.3") == "v1.2.3"

    def test_invalid_override_raises(self):
        with pytest.raises(ReleaseError, match="not v<major>"):
            version_from_tags(PUBLISHED_TAGS, "1.2.3")


class TestShortShaFromTags:
    def test_found(self):
        assert short_sha_from_tags(PUBLISHED_TAGS) == "1ce448c"

    def test_missing_raises(self):
        with pytest.raises(ReleaseError, match="Publish workflow"):
            short_sha_from_tags(["latest", "v0.1.16"])


class TestCheckSameBuild:
    def test_passes(self):
        check_same_build({"a": ["main-1ce448c"], "b": ["main-1ce448c", "x"]}, "1ce448c")

    def test_mismatch_names_the_repo(self):
        with pytest.raises(ReleaseError, match="^b is not tagged main-1ce448c"):
            check_same_build({"a": ["main-1ce448c"], "b": ["main-0000000"]}, "1ce448c")


class TestShouldPut:
    def test_absent(self):
        assert should_put("t", None, DIGEST) is True

    def test_same_digest(self):
        assert should_put("t", DIGEST, DIGEST) is False

    def test_other_digest_raises(self):
        with pytest.raises(ReleaseError, match="never moved"):
            should_put("t", OTHER_DIGEST, DIGEST)


def test_release_title_with_version():
    assert release_title(make_release()) == "release-v0.1.16 · 2026-09-24"


def test_release_title_without_version():
    assert release_title(make_release(release_tag=None)) == ORDERED


def test_image_ref():
    assert (
        image_ref(ACCOUNT, "eu-central-1", REPO, ORDERED)
        == f"{ACCOUNT}.dkr.ecr.eu-central-1.amazonaws.com/{REPO}:{ORDERED}"
    )


@pytest.mark.parametrize(
    "text", ["us-east-1\neu-central-1\n", "us-east-1 eu-central-1"]
)
def test_parse_list(text):
    assert parse_list(text) == ["us-east-1", "eu-central-1"]


class TestRenderNotes:
    def test_lists_both_tags_in_every_region(self):
        notes = render_notes(make_release(regions=("us-east-1", "eu-central-1")))
        assert notes.startswith("## release-v0.1.16 · 2026-09-24\n")
        assert DIGEST in notes and "1ce448c" in notes and "`main`" in notes
        for region in ("us-east-1", "eu-central-1"):
            assert image_ref(ACCOUNT, region, REPO, "release-v0.1.16") in notes
            assert image_ref(ACCOUNT, region, REPO, ORDERED) in notes

    def test_drops_release_tag_column_without_version(self):
        notes = render_notes(make_release(release_tag=None))
        assert "| Image | Digest | release-2026.09.24.153012 |" in notes
        assert "release-v" not in notes


class TestWriteOutputs:
    def test_with_version(self):
        out = write_outputs(make_release())
        assert out.splitlines() == [
            "release_tag=release-v0.1.16",
            f"ordered_tag={ORDERED}",
            "short_sha=1ce448c",
            "title=release-v0.1.16 · 2026-09-24",
            "digests=" + json.dumps({REPO: DIGEST}, separators=(",", ":")),
        ]

    def test_without_version(self):
        assert write_outputs(make_release(release_tag=None)).startswith(
            "release_tag=\n"
        )


# --- ECR ---------------------------------------------------------------------


class TestEcr:
    def test_describe_by_tag(self, stubbed):
        clients, stubbers = stubbed
        stub_describe(stubbers["us-east-1"], "main", tags=PUBLISHED_TAGS)
        image, tags = describe_by_tag(clients["us-east-1"], REPO, "main")
        assert image == Image(REPO, DIGEST, ACCOUNT)
        assert tags == PUBLISHED_TAGS

    def test_describe_by_tag_not_found(self, stubbed):
        clients, stubbers = stubbed
        stub_not_found(stubbers["us-east-1"], "main")
        with pytest.raises(ReleaseError, match=f"{REPO}:main not found in us-east-1"):
            describe_by_tag(clients["us-east-1"], REPO, "main")

    def test_digest_for_tag(self, stubbed):
        clients, stubbers = stubbed
        stub_describe(stubbers["us-east-1"], ORDERED)
        stub_not_found(stubbers["us-east-1"], "missing")
        assert digest_for_tag(clients["us-east-1"], REPO, ORDERED) == DIGEST
        assert digest_for_tag(clients["us-east-1"], REPO, "missing") is None

    def test_fetch_manifest(self, stubbed):
        clients, stubbers = stubbed
        stub_manifest(stubbers["us-east-1"])
        assert fetch_manifest(clients["us-east-1"], REPO, DIGEST) == (
            MANIFEST,
            MEDIA_TYPE,
        )

    @pytest.mark.parametrize(
        "response",
        [
            pytest.param({"images": [], "failures": []}, id="empty"),
            pytest.param(
                {
                    "images": [],
                    "failures": [
                        {
                            "imageId": {"imageDigest": DIGEST},
                            "failureCode": "ImageNotFound",
                            "failureReason": "Requested image not found",
                        }
                    ],
                },
                id="failures",
            ),
        ],
    )
    def test_fetch_manifest_not_replicated(self, stubbed, response):
        clients, stubbers = stubbed
        stubbers["eu-central-1"].add_response("batch_get_image", response)
        with pytest.raises(ReleaseError, match="not replicated to eu-central-1"):
            fetch_manifest(clients["eu-central-1"], REPO, DIGEST)


class TestRetagEverywhere:
    def test_puts_a_fresh_tag_by_digest(self, stubbed, capsys):
        clients, stubbers = stubbed
        for s in stubbers.values():
            stub_manifest(s)
            stub_not_found(s, ORDERED)
            stub_put(s, ORDERED)
        retag_everywhere(clients, Image(REPO, DIGEST, ACCOUNT), [ORDERED])
        assert capsys.readouterr().out.count("TAGGED") == 2

    def test_skips_a_tag_already_on_the_digest(self, stubbed, capsys):
        clients, stubbers = stubbed
        for s in stubbers.values():
            stub_manifest(s)
            stub_describe(s, ORDERED)
        retag_everywhere(clients, Image(REPO, DIGEST, ACCOUNT), [ORDERED])
        assert capsys.readouterr().out.count("OK:") == 2

    def test_refuses_to_move_a_tag(self, stubbed):
        clients, stubbers = stubbed
        stub_manifest(stubbers["us-east-1"])
        stub_describe(stubbers["us-east-1"], ORDERED, digest=OTHER_DIGEST)
        with pytest.raises(ReleaseError, match="never moved"):
            retag_everywhere(
                {"us-east-1": clients["us-east-1"]},
                Image(REPO, DIGEST, ACCOUNT),
                [ORDERED],
            )

    def test_fails_when_a_region_lacks_the_digest(self, stubbed):
        clients, stubbers = stubbed
        stub_manifest(stubbers["us-east-1"])
        stub_describe(stubbers["us-east-1"], ORDERED)
        stubbers["eu-central-1"].add_response(
            "batch_get_image", {"images": [], "failures": []}
        )
        with pytest.raises(ReleaseError, match="not replicated to eu-central-1"):
            retag_everywhere(clients, Image(REPO, DIGEST, ACCOUNT), [ORDERED])


# --- run() end to end --------------------------------------------------------


def run_release(clients, repositories=(REPO,)):
    return run(
        clients=clients,
        repositories=list(repositories),
        source_tag="main",
        version_override="",
        now=NOW,
    )


class TestRun:
    def test_first_release(self, stubbed):
        clients, stubbers = stubbed
        stub_describe(stubbers["us-east-1"], "main", tags=PUBLISHED_TAGS)
        for s in stubbers.values():
            stub_manifest(s)
            for tag in ("release-v0.1.16", ORDERED):
                stub_not_found(s, tag)
                stub_put(s, tag)

        release = run_release(clients)

        assert release == Release(
            source_tag="main",
            short_sha="1ce448c",
            release_tag="release-v0.1.16",
            ordered_tag=ORDERED,
            regions=["us-east-1", "eu-central-1"],
            images=[Image(REPO, DIGEST, ACCOUNT)],
        )

    def test_rerun_reuses_the_ordered_tag(self, stubbed):
        clients, stubbers = stubbed
        earlier = "release-2026.09.20.080000"
        stub_describe(
            stubbers["us-east-1"],
            "main",
            tags=[*PUBLISHED_TAGS, "release-v0.1.16", earlier],
        )
        for s in stubbers.values():
            stub_manifest(s)
            stub_describe(s, "release-v0.1.16")
            stub_describe(s, earlier)

        assert run_release(clients).ordered_tag == earlier

    def test_without_version_uses_only_the_ordered_tag(self, stubbed):
        clients, stubbers = stubbed
        stub_describe(stubbers["us-east-1"], "main", tags=["main", "main-1ce448c"])
        for s in stubbers.values():
            stub_manifest(s)
            stub_not_found(s, ORDERED)
            stub_put(s, ORDERED)

        assert run_release(clients).release_tag is None

    def test_images_from_different_builds_fail(self, stubbed):
        clients, stubbers = stubbed
        stub_describe(stubbers["us-east-1"], "main", tags=PUBLISHED_TAGS)
        stubbers["us-east-1"].add_response(
            "describe_images",
            {
                "imageDetails": [
                    {
                        "registryId": ACCOUNT,
                        "repositoryName": "other",
                        "imageDigest": OTHER_DIGEST,
                        "imageTags": ["main", "main-0000000"],
                    }
                ]
            },
            {"repositoryName": "other", "imageIds": [{"imageTag": "main"}]},
        )
        with pytest.raises(ReleaseError, match="^other is not tagged main-1ce448c"):
            run_release(clients, repositories=(REPO, "other"))


# --- main() ------------------------------------------------------------------


class TestMain:
    @pytest.fixture
    def env(self, monkeypatch):
        monkeypatch.setenv("REPOSITORIES", f"{REPO}\n")
        monkeypatch.setenv("REGIONS", "us-east-1 eu-central-1")
        monkeypatch.setenv("SOURCE_TAG", "")
        monkeypatch.setenv("VERSION", "")
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        monkeypatch.delenv("NOTES_FILE", raising=False)

    @pytest.fixture
    def fake_run(self, monkeypatch):
        calls = []

        def fake(**kwargs):
            calls.append(kwargs)
            return make_release()

        monkeypatch.setattr(ecr_release, "run", fake)
        return calls

    def test_writes_outputs_summary_and_notes(
        self, env, fake_run, monkeypatch, tmp_path
    ):
        output, summary = tmp_path / "output", tmp_path / "summary"
        notes = tmp_path / "notes.md"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        monkeypatch.setenv("NOTES_FILE", str(notes))

        main()

        (kwargs,) = fake_run
        assert list(kwargs["clients"]) == ["us-east-1", "eu-central-1"]
        assert kwargs["repositories"] == [REPO]
        assert kwargs["source_tag"] == "main"
        assert kwargs["now"].tzinfo == timezone.utc
        assert output.read_text(encoding="utf-8") == write_outputs(make_release())
        assert summary.read_text(encoding="utf-8") == render_notes(make_release())
        assert notes.read_text(encoding="utf-8") == render_notes(make_release())

    def test_prints_without_github_files(self, env, fake_run, capsys):
        main()
        out = capsys.readouterr().out
        assert f"ordered_tag={ORDERED}" in out
        assert "| Image | Digest |" in out

    def test_release_error_exits_1(self, env, monkeypatch, capsys):
        def fail(**kwargs):
            raise ReleaseError("nope")

        monkeypatch.setattr(ecr_release, "run", fail)
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        assert "::error::nope" in capsys.readouterr().out

    def test_requires_regions(self, env, fake_run, monkeypatch, capsys):
        monkeypatch.setenv("REGIONS", "")
        with pytest.raises(SystemExit):
            main()
        assert "::error::at least one region" in capsys.readouterr().out
        assert fake_run == []
