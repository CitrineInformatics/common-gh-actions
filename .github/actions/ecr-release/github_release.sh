#!/usr/bin/env bash
# Validate GitHub before ECR writes, then record the release after ECR succeeds.
set -euo pipefail

PHASE="${1:?Usage: github_release.sh <preflight|record>}"
case "$PHASE" in
  preflight) SHA="$(gh api "repos/${GH_REPO}/commits/${SHORT_SHA}" --jq .sha)" ;;
  record) : "${SHA:?The preflight commit SHA is required}" ;;
  *) echo "::error::Unknown GitHub release phase: ${PHASE}"; exit 1 ;;
esac

# matching-refs returns an empty list for a missing tag. Other API failures
# propagate, so a permissions or network error cannot be mistaken for absence.
VERSION_EXISTS=false
for TAG in "${RELEASE_TAG}" "${ORDERED_TAG}"; do
  [[ -n "${TAG}" ]] || continue
  REF="$(gh api "repos/${GH_REPO}/git/matching-refs/tags/${TAG}" \
    --jq ".[] | select(.ref == \"refs/tags/${TAG}\") | .object | [.type, .sha] | @tsv")"
  [[ -n "${REF}" ]] || continue
  read -r TYPE EXISTING <<< "${REF}"
  # Dereference annotated tags as well as lightweight tags.
  while [[ "${TYPE}" == tag ]]; do
    REF="$(gh api "repos/${GH_REPO}/git/tags/${EXISTING}" --jq '.object | [.type, .sha] | @tsv')"
    read -r TYPE EXISTING <<< "${REF}"
  done
  if [[ "${TYPE}" != commit || "${EXISTING}" != "${SHA}" ]]; then
    echo "::error::git tag ${TAG} already on ${EXISTING}, not ${SHA}; a release tag is never moved"
    exit 1
  fi
  [[ "${TAG}" != "${RELEASE_TAG}" ]] || VERSION_EXISTS=true
  echo "OK: git tag ${TAG} already on ${SHA}"
done

# Read release history during preflight too, before ECR writes.
TAGS="$(gh release list --limit 100 --json tagName --jq '.[].tagName')"
if [[ "${PHASE}" == preflight ]]; then
  echo "sha=${SHA}" >> "${GITHUB_OUTPUT}"
  exit 0
fi

if [[ -n "${RELEASE_TAG}" && "${VERSION_EXISTS}" == false ]]; then
  gh api -X POST "repos/${GH_REPO}/git/refs" -f "ref=refs/tags/${RELEASE_TAG}" -f "sha=${SHA}" > /dev/null
  echo "CREATED: git tag ${RELEASE_TAG} -> ${SHA}"
fi

if gh release view "${ORDERED_TAG}" > /dev/null 2>&1; then
  echo "OK: GitHub release ${ORDERED_TAG} already exists"
  exit 0
fi

PREVIOUS=""
while IFS= read -r TAG; do
  if [[ "${TAG}" == release-[0-9]* && "${TAG}" < "${ORDERED_TAG}" && "${TAG}" > "${PREVIOUS}" ]]; then
    PREVIOUS="${TAG}"
  fi
done <<< "${TAGS}"

ARGS=(--target "${SHA}" --title "${TITLE}" --notes-file "${NOTES_FILE}" --generate-notes --latest)
if [[ -n "${PREVIOUS}" ]]; then
  ARGS+=(--notes-start-tag "${PREVIOUS}")
fi
gh release create "${ORDERED_TAG}" "${ARGS[@]}" > /dev/null
echo "CREATED: GitHub release ${ORDERED_TAG}"
