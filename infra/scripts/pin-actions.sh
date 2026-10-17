#!/bin/sh
# Rewrite `uses: owner/repo@vN` in .github/workflows to full commit SHAs (keeps the tag in a
# comment). Needs an authenticated `gh` with network access. Review the diff, then commit.
#   sh infra/scripts/pin-actions.sh
set -eu
cd "$(dirname "$0")/../.."
for f in .github/workflows/*.yml; do
  grep -oE 'uses: [A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@v[0-9][0-9.]*' "$f" | sort -u | while read -r _ ref; do
    repo=${ref%@*}; tag=${ref#*@}
    sha=$(gh api "repos/${repo%%/[!/]*}/commits/$tag" --jq .sha 2>/dev/null || true)
    [ -n "$sha" ] || { echo "skip $ref" >&2; continue; }
    sed -i "s|uses: $ref\$|uses: $repo@$sha # $tag|" "$f"
  done
done
