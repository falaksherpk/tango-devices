#!/usr/bin/env bash
# Publish beamline-tango-devices' two .debs to the lab's apt repository (handbook Part 4,
# Ch34). Runs in the publish-deb job, on pkg.beamline as gitlab-runner, for a protected
# vX.Y.Z tag. Usage: ci/publish-deb.sh DEBDIR
#
# Devices first, then simulators. If the repository already has a package at this
# version, the published file must be byte-identical (the SHA256 in the published
# Packages index): then it is skipped, otherwise the job fails. That makes a re-run
# after a partial failure safe; beamline-publish itself never publishes a version twice.
# A new version is copied into the drop directory and published as the repository
# owner through the one sudo rule (beamline-publish signs and exports); the copy is
# removed either way. Afterwards the index must list both with the built files' hashes.
set -euo pipefail
shopt -s nullglob

debs=${1:?usage: $0 DEBDIR}
drop=${PUBLISH_DROP:-/var/spool/beamline-publish}
index=${PUBLISH_INDEX:-/srv/beamline-repo/apt/dists/noble/main/binary-amd64/Packages}
owner=falak

refuse() { echo "refusing: $*" >&2; exit 1; }

# The SHA256 the index lists for PACKAGE at VERSION, or nothing.
published_sha() {
  awk -v p="$1" -v v="$2" '
    function flush() { if (pk == p && ve == v) print sh; pk = ve = sh = "" }
    /^Package: / { pk = $2 } /^Version: / { ve = $2 } /^SHA256: / { sh = $2 }
    /^$/ { flush() } END { flush() }' "$index"
}

all=("$debs"/*.deb)
dev=("$debs"/beamline-tango-devices_*_amd64.deb)
sim=("$debs"/beamline-tango-simulators_*_all.deb)
(( ${#all[@]} == 2 && ${#dev[@]} == 1 && ${#sim[@]} == 1 )) ||
  refuse "expected exactly the devices (amd64) and simulators (all) .debs in $debs, found: ${all[*]:-none}"
[[ -r $index ]] || refuse "cannot read the published index $index"

want=${CI_COMMIT_TAG:+${CI_COMMIT_TAG#v}}
copy=
trap 'rm -f -- "$copy"' EXIT
for f in "${dev[0]}" "${sim[0]}"; do
  name=$(dpkg-deb --field "$f" Package)
  version=$(dpkg-deb --field "$f" Version)
  sha=$(sha256sum < "$f" | cut -d' ' -f1)
  [[ -z $want || $version == "$want" ]] || refuse "$name is version $version, the tag says $want"
  published=$(published_sha "$name" "$version")
  if [[ -n $published ]]; then
    [[ $published == "$sha" ]] ||
      refuse "$name $version is already published with SHA256 $published; this build is $sha"
    echo "$name $version: already published, byte-identical ($sha); skipped"
    continue
  fi
  target=$drop/$(basename "$f")
  [[ ! -e $target ]] || refuse "$target already exists (left behind by another job?)"
  # Only a copy this job made is ever removed (by the trap too).
  install -m 0640 "$f" "$target"
  copy=$target
  rc=0
  sudo -n -u "$owner" /usr/local/bin/beamline-publish deb "$copy" || rc=$?
  rm -f -- "$copy"
  copy=
  (( rc == 0 )) || refuse "beamline-publish failed for $name $version (exit $rc)"
  echo "$name $version: published ($sha)"
done

for f in "${dev[0]}" "${sim[0]}"; do
  name=$(dpkg-deb --field "$f" Package)
  version=$(dpkg-deb --field "$f" Version)
  sha=$(sha256sum < "$f" | cut -d' ' -f1)
  [[ $(published_sha "$name" "$version") == "$sha" ]] ||
    refuse "after publishing, the index does not list $name $version with SHA256 $sha"
done
echo "index $index lists both packages with the built files' hashes"
