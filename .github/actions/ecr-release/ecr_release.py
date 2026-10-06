"""Mark a published ECR image as a release, without rebuilding it.

The image's digest is resolved once in the first region, then tagged
``release-v<version>`` (when the image carries a version) and
``release-YYYY.MM.DD.HHMMSS`` in every region. A release tag is never moved, so
re-running a release is a no-op. Planning checks all destinations without
writing; the action validates GitHub before applying the saved plan.
"""

import json
import os
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ORDERED_FORMAT = "release-%Y.%m.%d.%H%M%S"

# Asking for every type returns the manifest exactly as stored, index or not.
MANIFEST_MEDIA_TYPES = [
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
]


class ReleaseError(Exception):
    """A failure the caller should read, reported without a traceback."""


@dataclass(frozen=True)
class Image:
    repository: str
    digest: str
    registry_id: str


@dataclass(frozen=True)
class Release:
    source_tag: str
    short_sha: str
    release_tag: str | None
    ordered_tag: str
    regions: list[str]
    images: list[Image]


@dataclass(frozen=True)
class Manifest:
    repository: str
    digest: str
    region: str
    content: str
    media_type: str


@dataclass(frozen=True)
class Plan:
    release: Release
    manifests: list[Manifest]


def run(
    clients: dict,
    repositories: list[str],
    source_tag: str,
    version_override: str,
    now: datetime,
) -> Plan:
    """Resolve immutable sources and preflight every ECR destination without writing."""
    if not (is_sha_tag(source_tag) or is_version_tag(source_tag)):
        raise ReleaseError(
            "source tag must be main-<sha> or v<major>.<minor>.<n>; mutable tags and empty inputs cannot be released"
        )
    regions = list(clients)
    first = clients[regions[0]]
    resolved = [describe_by_tag(first, repo, source_tag) for repo in repositories]
    images = [image for image, _ in resolved]
    all_tags = [tag for _, tags in resolved for tag in tags]
    short_sha = short_sha_from_tags(all_tags, source_tag)
    check_same_build({image.repository: tags for image, tags in resolved}, short_sha)
    release = Release(
        source_tag=source_tag,
        short_sha=short_sha,
        release_tag=release_tag_for(
            version_from_tags(all_tags, version_override, source_tag)
        ),
        ordered_tag=choose_ordered_tag(all_tags, now),
        regions=regions,
        images=images,
    )
    manifests = []
    for image in images:
        for region, client in clients.items():
            content, media_type = fetch_manifest(client, image.repository, image.digest)
            manifests.append(
                Manifest(image.repository, image.digest, region, content, media_type)
            )
    check_destinations(clients, release, manifests)
    return Plan(release, manifests)


def release_tags(release: Release) -> list[str]:
    return [tag for tag in (release.release_tag, release.ordered_tag) if tag]


def check_destinations(
    clients: dict, release: Release, manifests: list[Manifest]
) -> list[tuple[Manifest, str]]:
    pending = []
    for manifest in manifests:
        client = clients[manifest.region]
        for tag in release_tags(release):
            existing = digest_for_tag(client, manifest.repository, tag)
            if should_put(tag, existing, manifest.digest):
                pending.append((manifest, tag))
    return pending


def apply_plan(clients: dict, plan: Plan) -> None:
    # Recheck every destination before any writes, using the digests selected by preflight.
    pending = check_destinations(clients, plan.release, plan.manifests)
    for manifest, tag in pending:
        clients[manifest.region].put_image(
            repositoryName=manifest.repository,
            imageManifest=manifest.content,
            imageManifestMediaType=manifest.media_type,
            imageTag=tag,
            imageDigest=manifest.digest,
        )
        print(
            f"TAGGED: {manifest.repository}:{tag} -> {manifest.digest} in {manifest.region}"
        )


def load_plan(path: Path) -> Plan:
    data = json.loads(path.read_text(encoding="utf-8"))
    release = data["release"]
    release["images"] = [Image(**image) for image in release["images"]]
    return Plan(
        Release(**release), [Manifest(**manifest) for manifest in data["manifests"]]
    )


def main() -> None:
    try:
        plan_file = os.environ.get("PLAN_FILE", "")
        if not plan_file:
            raise ReleaseError("PLAN_FILE is required")
        phase = os.environ.get("RELEASE_PHASE", "plan")
        if phase == "plan":
            regions = parse_list(os.environ.get("REGIONS", ""))
            repositories = parse_list(os.environ.get("REPOSITORIES", ""))
            if not regions or not repositories:
                raise ReleaseError(
                    "at least one region and one repository are required"
                )
            plan = run(
                clients={r: boto3.client("ecr", region_name=r) for r in regions},
                repositories=repositories,
                source_tag=os.environ.get("SOURCE_TAG", ""),
                version_override=os.environ.get("VERSION", ""),
                now=datetime.now(UTC),
            )
            Path(plan_file).write_text(json.dumps(asdict(plan)), encoding="utf-8")
            write_or_print(os.environ.get("GITHUB_OUTPUT"), write_outputs(plan.release))
            if notes_file := os.environ.get("NOTES_FILE"):
                Path(notes_file).write_text(
                    render_notes(plan.release), encoding="utf-8"
                )
        elif phase == "apply":
            plan = load_plan(Path(plan_file))
            apply_plan(
                {r: boto3.client("ecr", region_name=r) for r in plan.release.regions},
                plan,
            )
            write_or_print(
                os.environ.get("GITHUB_STEP_SUMMARY"), render_notes(plan.release)
            )
        else:
            raise ReleaseError(f"unknown release phase {phase!r}")
    except (ReleaseError, ClientError) as e:
        print(f"::error::{e}")
        sys.exit(1)


def write_or_print(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)


# --- ECR ---------------------------------------------------------------------


def describe_by_tag(client, repo: str, tag: str) -> tuple[Image, list[str]]:
    """The image ``repo:tag`` and every tag it carries."""
    try:
        response = client.describe_images(
            repositoryName=repo, imageIds=[{"imageTag": tag}]
        )
    except client.exceptions.ImageNotFoundException:
        raise ReleaseError(
            f"{repo}:{tag} not found in {client.meta.region_name}"
        ) from None
    detail = response["imageDetails"][0]
    image = Image(repo, detail["imageDigest"], detail["registryId"])
    return image, detail.get("imageTags", [])


def digest_for_tag(client, repo: str, tag: str) -> str | None:
    try:
        response = client.describe_images(
            repositoryName=repo, imageIds=[{"imageTag": tag}]
        )
    except client.exceptions.ImageNotFoundException:
        return None
    return response["imageDetails"][0]["imageDigest"]


def fetch_manifest(client, repo: str, digest: str) -> tuple[str, str]:
    """The manifest and its media type, fetched by digest so a moving tag can't race."""
    response = client.batch_get_image(
        repositoryName=repo,
        imageIds=[{"imageDigest": digest}],
        acceptedMediaTypes=MANIFEST_MEDIA_TYPES,
    )
    if response.get("failures") or not response.get("images"):
        raise ReleaseError(
            f"{repo}@{digest} not replicated to {client.meta.region_name}"
        )
    image = response["images"][0]
    return image["imageManifest"], image["imageManifestMediaType"]


# --- Pure helpers ------------------------------------------------------------


def parse_list(text: str) -> list[str]:
    """Split a newline- or space-separated input."""
    return text.split()


def is_version_tag(tag: str) -> bool:
    """True for ``v<major>.<minor>.<n>``, the tag the Publish workflow adds."""
    parts = tag[1:].split(".")
    return tag.startswith("v") and len(parts) == 3 and all(p.isdigit() for p in parts)


def is_ordered_tag(tag: str) -> bool:
    try:
        datetime.strptime(tag, ORDERED_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return False
    return True


def ordered_tag_for(now: datetime) -> str:
    return now.astimezone(UTC).strftime(ORDERED_FORMAT)


def choose_ordered_tag(existing: Iterable[str], now: datetime) -> str:
    """Reuse the ordered tag an earlier release left on the image, else mint one."""
    return min((t for t in existing if is_ordered_tag(t)), default=ordered_tag_for(now))


def is_sha_tag(tag: str) -> bool:
    sha = tag.removeprefix("main-")
    return (
        tag.startswith("main-")
        and 7 <= len(sha) <= 40
        and all(c in "0123456789abcdef" for c in sha)
    )


def version_from_tags(
    tags: list[str], override: str, source_tag: str = ""
) -> str | None:
    if override:
        if not is_version_tag(override):
            raise ReleaseError(f"version {override!r} is not v<major>.<minor>.<n>")
        return override
    if is_version_tag(source_tag):
        return source_tag
    versions = sorted({tag for tag in tags if is_version_tag(tag)})
    if len(versions) > 1:
        raise ReleaseError(
            f"ambiguous image versions: {', '.join(versions)}; select a version source tag or provide a version override"
        )
    return versions[0] if versions else None


def short_sha_from_tags(tags: list[str], source_tag: str = "") -> str:
    if is_sha_tag(source_tag):
        return source_tag.removeprefix("main-")
    shas = sorted({tag.removeprefix("main-") for tag in tags if is_sha_tag(tag)})
    if not shas:
        raise ReleaseError(
            f"image tagged {', '.join(tags)} carries no main-<sha> tag; "
            "only images from the Publish workflow can be released"
        )
    if len(shas) > 1:
        raise ReleaseError(
            f"ambiguous image commits: {', '.join(shas)}; select a main-<sha> source tag"
        )
    return shas[0]


def check_same_build(tags_by_repo: dict[str, list[str]], short_sha: str) -> None:
    for repo, tags in tags_by_repo.items():
        if f"main-{short_sha}" not in tags:
            raise ReleaseError(
                f"{repo} is not tagged main-{short_sha}; images come from different builds"
            )


def release_tag_for(version: str | None) -> str | None:
    return f"release-{version}" if version else None


def should_put(tag: str, existing_digest: str | None, digest: str) -> bool:
    if existing_digest is None:
        return True
    if existing_digest == digest:
        return False
    raise ReleaseError(
        f"{tag} already on {existing_digest}, not {digest}; a release tag is never moved"
    )


def release_title(release: Release) -> str:
    if not release.release_tag:
        return release.ordered_tag
    released_on = datetime.strptime(release.ordered_tag, ORDERED_FORMAT).replace(
        tzinfo=UTC
    )
    return f"{release.release_tag} · {released_on:%Y-%m-%d}"


def image_ref(registry_id: str, region: str, repo: str, tag: str) -> str:
    return f"{registry_id}.dkr.ecr.{region}.amazonaws.com/{repo}:{tag}"


def render_notes(release: Release) -> str:
    tags = [t for t in (release.release_tag, release.ordered_tag) if t]
    header = ["Image", "Digest", *tags]
    rows = [
        [
            image.repository,
            f"`{image.digest}`",
            *(
                f"`{image_ref(image.registry_id, region, image.repository, t)}`"
                for t in tags
            ),
        ]
        for image in release.images
        for region in release.regions
    ]
    table = [header, ["---"] * len(header), *rows]
    return "\n".join(
        [
            f"## {release_title(release)}",
            "",
            f"Released from image tag `{release.source_tag}` at commit {release.short_sha}.",
            "",
            *("| " + " | ".join(cells) + " |" for cells in table),
            "",
        ]
    )


def write_outputs(release: Release) -> str:
    digests = {image.repository: image.digest for image in release.images}
    return "".join(
        [
            f"release_tag={release.release_tag or ''}\n",
            f"ordered_tag={release.ordered_tag}\n",
            f"short_sha={release.short_sha}\n",
            f"title={release_title(release)}\n",
            f"digests={json.dumps(digests, separators=(',', ':'))}\n",
        ]
    )


if __name__ == "__main__":  # pragma: no cover
    main()
