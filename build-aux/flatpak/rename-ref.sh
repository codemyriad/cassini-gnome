#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Mark an existing old ref as superseded, only after its replacement exists.
# This changes a local repository, never a live remote or an installation.
# Usage: rename-ref.sh REPOSITORY [--devel]
set -eu
repo=$1
old=com.gocassini.Viewer
new=io.codemyriad.CassiniTalk
case "${2:-}" in
  '') ;;
  --devel) old=$old.Devel; new=$new.Devel ;;
  *) echo 'Expected --devel or no profile argument' >&2; exit 2 ;;
esac
arch=$(flatpak --default-arch)
old_ref=app/$old/$arch/master
new_ref=app/$new/$arch/master
refs=$(flatpak repo --branches "$repo")
for ref in "$old_ref" "$new_ref"; do
  if ! printf '%s\n' "$refs" | cut -f1 | grep -Fxq "$ref"; then
    echo "Required ref is absent: $ref" >&2
    exit 1
  fi
done
flatpak build-commit-from --force --src-ref="$old_ref" \
  --end-of-life-rebase="$old=$new" "$repo" "$old_ref"
flatpak build-update-repo "$repo"
