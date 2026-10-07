#!/usr/bin/env bash
# Tag a published image as a release in every region, then record a GitHub release.
# Re-running with the same source tag completes a partial release: ECR rejects a tag
# already on the image, the ordered tag is reused, and GitHub objects are created only if absent.
set -euo pipefail
: "${REPOSITORIES:?}" "${REGIONS:?}" "${SOURCE_TAG:?}" "${GH_REPO:?}"

read -r -d '' -a REPOS <<< "${REPOSITORIES}" || true
read -r -d '' -a REGION_LIST <<< "${REGIONS}" || true
MEDIA_TYPES=(
  application/vnd.docker.distribution.manifest.v2+json
  application/vnd.docker.distribution.manifest.list.v2+json
  application/vnd.oci.image.manifest.v1+json
  application/vnd.oci.image.index.v1+json
)

if [[ "${SOURCE_TAG}" != main-* && "${SOURCE_TAG}" != v* ]]; then
  echo "::error::source tag must be main-<sha> or v<major.minor>.<n>, not '${SOURCE_TAG}'"
  exit 1
fi

describe() {
  aws ecr describe-images --region "${REGION_LIST[0]}" --repository-name "$1" \
    --image-ids imageTag="${SOURCE_TAG}" --query "imageDetails[0].$2" --output text
}

TAGS="$(describe "${REPOS[0]}" imageTags | tr '\t' '\n')"
VERSION="$(grep -m1 '^v[0-9]' <<< "${TAGS}" || true)"
SHORT_SHA="$(sed -n 's/^main-//p' <<< "${TAGS}" | head -1)"
ORDERED_TAG="$(grep '^release-20' <<< "${TAGS}" | sort | head -1 || true)"
ORDERED_TAG="${ORDERED_TAG:-$(date -u +release-%Y.%m.%d.%H%M%S)}"
if [[ -z "${SHORT_SHA}" ]]; then
  echo "::error::${REPOS[0]}:${SOURCE_TAG} has no main-<sha> tag; only images from Publish can be released"
  exit 1
fi
RELEASE_TAGS=(${VERSION:+"release-${VERSION}"} "${ORDERED_TAG}")

NOTES="Released \`${SOURCE_TAG}\` (commit ${SHORT_SHA}) in ${REGION_LIST[*]} with tags ${RELEASE_TAGS[*]}."$'\n'
for REPO in "${REPOS[@]}"; do
  DIGEST="$(describe "${REPO}" imageDigest)"
  NOTES+=$'\n'"- \`${REPO}@${DIGEST}\`"
  for REGION in "${REGION_LIST[@]}"; do
    # Fetch by digest so every region tags the same image.
    IMAGE="$(aws ecr batch-get-image --region "${REGION}" --repository-name "${REPO}" \
      --image-ids imageDigest="${DIGEST}" --accepted-media-types "${MEDIA_TYPES[@]}" \
      --query 'images[0]' --output json)"
    MANIFEST="$(jq -r '.imageManifest // empty' <<< "${IMAGE}")"
    if [[ -z "${MANIFEST}" ]]; then
      echo "::error::${REPO}@${DIGEST} not found in ${REGION}"
      exit 1
    fi
    for TAG in "${RELEASE_TAGS[@]}"; do
      if ERR="$(aws ecr put-image --region "${REGION}" --repository-name "${REPO}" \
        --image-tag "${TAG}" --image-digest "${DIGEST}" --image-manifest "${MANIFEST}" \
        --image-manifest-media-type "$(jq -r .imageManifestMediaType <<< "${IMAGE}")" 2>&1 > /dev/null)"; then
        echo "TAGGED: ${REPO}:${TAG} in ${REGION}"
      elif [[ "${ERR}" == *ImageAlreadyExistsException* ]]; then
        echo "OK: ${REPO}:${TAG} already in ${REGION}"
      else
        echo "${ERR}" >&2
        exit 1
      fi
    done
  done
done

SHA="$(gh api "repos/${GH_REPO}/commits/${SHORT_SHA}" --jq .sha)"
if [[ -n "${VERSION}" ]] && ! gh api "repos/${GH_REPO}/git/ref/tags/release-${VERSION}" > /dev/null 2>&1; then
  gh api -X POST "repos/${GH_REPO}/git/refs" -f "ref=refs/tags/release-${VERSION}" -f "sha=${SHA}" > /dev/null
fi
if gh release view "${ORDERED_TAG}" > /dev/null 2>&1; then
  echo "OK: GitHub release ${ORDERED_TAG} already exists"
else
  gh release create "${ORDERED_TAG}" --target "${SHA}" --title "${RELEASE_TAGS[0]}" \
    --notes "${NOTES}" --generate-notes --latest > /dev/null
  echo "CREATED: GitHub release ${ORDERED_TAG}"
fi
echo "${NOTES}" >> "${GITHUB_STEP_SUMMARY:-/dev/stdout}"
