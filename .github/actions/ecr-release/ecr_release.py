"""Mark a published ECR image as a release, without rebuilding it.

The image's digest is resolved once in the first region, then tagged
``release-v<version>`` (when the image carries a version) and
``release-YYYY.MM.DD.HHMMSS`` in every region. A release tag is never moved, so
re-running a release is a no-op. The action's next step records the GitHub
release from this script's outputs.
"""

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

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


def run(
    clients: dict,
    repositories: list[str],
    source_tag: str,
    version_override: str,
    now: datetime,
) -> Release:
    """Release the images tagged ``source_tag``; the first client's region is the source of truth."""
    regions = list(clients)
    first = clients[regions[0]]

    resolved = [describe_by_tag(first, repo, source_tag) for repo in repositories]
    images = [image for image, _ in resolved]
    first_tags = resolved[0][1]

    version = version_from_tags(first_tags, version_override)
    short_sha = short_sha_from_tags(first_tags)
    check_same_build({image.repository: tags for image, tags in resolved}, short_sha)

    ordered_tag = choose_ordered_tag((tag for _, tags in resolved for tag in tags), now)
    release_tag = release_tag_for(version)
    new_tags = [tag for tag in (release_tag, ordered_tag) if tag]
    for image in images:
        retag_everywhere(clients, image, new_tags)

    return Release(
        source_tag=source_tag,
        short_sha=short_sha,
        release_tag=release_tag,
        ordered_tag=ordered_tag,
        regions=regions,
        images=images,
    )


def main() -> None:
    regions = parse_list(os.environ.get("REGIONS", ""))
    repositories = parse_list(os.environ.get("REPOSITORIES", ""))
    try:
        if not regions or not repositories:
            raise ReleaseError("at least one region and one repository are required")
        release = run(
            clients={r: boto3.client("ecr", region_name=r) for r in regions},
            repositories=repositories,
            source_tag=os.environ.get("SOURCE_TAG") or "main",
            version_override=os.environ.get("VERSION", ""),
            now=datetime.now(timezone.utc),
        )
    except (ReleaseError, ClientError) as e:
        print(f"::error::{e}")
        sys.exit(1)

    notes = render_notes(release)
    write_or_print(os.environ.get("GITHUB_OUTPUT"), write_outputs(release))
    write_or_print(os.environ.get("GITHUB_STEP_SUMMARY"), notes)
    # The GitHub release step reads its notes from here.
    if notes_file := os.environ.get("NOTES_FILE"):
        Path(notes_file).write_text(notes, encoding="utf-8")


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


def retag_everywhere(clients: dict, image: Image, tags: list[str]) -> None:
    for region, client in clients.items():
        manifest, media_type = fetch_manifest(client, image.repository, image.digest)
        for tag in tags:
            ref = f"{image.repository}:{tag}"
            existing = digest_for_tag(client, image.repository, tag)
            if not should_put(tag, existing, image.digest):
                print(f"OK: {ref} already on {image.digest} in {region}")
                continue
            client.put_image(
                repositoryName=image.repository,
                imageManifest=manifest,
                imageManifestMediaType=media_type,
                imageTag=tag,
                # ECR rejects the put unless the manifest hashes to this digest.
                imageDigest=image.digest,
            )
            print(f"TAGGED: {ref} -> {image.digest} in {region}")


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
        datetime.strptime(tag, ORDERED_FORMAT)
    except ValueError:
        return False
    return True


def ordered_tag_for(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime(ORDERED_FORMAT)


def choose_ordered_tag(existing: Iterable[str], now: datetime) -> str:
    """Reuse the ordered tag an earlier release left on the image, else mint one."""
    return min((t for t in existing if is_ordered_tag(t)), default=ordered_tag_for(now))


def version_from_tags(tags: list[str], override: str) -> str | None:
    if override:
        if not is_version_tag(override):
            raise ReleaseError(f"version {override!r} is not v<major>.<minor>.<n>")
        return override
    return next((t for t in tags if is_version_tag(t)), None)


def short_sha_from_tags(tags: list[str]) -> str:
    sha = next((t.removeprefix("main-") for t in tags if t.startswith("main-")), None)
    if sha is None:
        raise ReleaseError(
            f"image tagged {', '.join(tags)} carries no main-<sha> tag; "
            "only images from the Publish workflow can be released"
        )
    return sha


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
    released_on = datetime.strptime(release.ordered_tag, ORDERED_FORMAT)
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
