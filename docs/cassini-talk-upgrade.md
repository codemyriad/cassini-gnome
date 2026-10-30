# Cassini talk identity upgrade (D45)

The public application ID is `io.codemyriad.CassiniTalk` (development builds
append `.Devel`). The native package and command are `cassini-talk`.
`cassini-viewer` remains a compatibility command. Internal Python imports,
gettext domain and private model/payload paths keep their existing names.
The Library stays in `Music/Cassini` and is not moved.

Debian uses the native-package revision `0.3.0+talk1`, retaining application
version 0.3.0. Its architecture-independent `cassini-viewer` transition package
depends on the exact `cassini-talk` version. RPM and Arch use package release 2
to supersede their existing 0.3.0 release 1. This PR does not tag or publish a
new application release.

The new GSettings schema copies explicit user values from the old schema once.
Values already saved under the new identity take precedence. The compatibility
schema remains installed; the old settings are not deleted. Portal permissions
belong to the new application identity and must be requested again. They are
not copied from the portal's permission store.

## Flatpak repository migration

D45.1 fixes the production origin to
`https://cassini.talk/download/linux/flatpak/`. Build a stable release locally:

```sh
build-aux/flatpak/build.sh --release \
  --gpg-key=FULL_PUBLIC_SIGNING_KEY_FINGERPRINT \
  --gpg-homedir="$HOME/.config/cassini/secrets/flatpak-gnupg" \
  --legacy-bundle=/path/to/released/cassini-viewer.flatpak
```

For an already-built local repository, use the same preparation step directly:

```sh
python3 build-aux/flatpak/prepare-release.py repo dist \
  --key=FULL_PUBLIC_SIGNING_KEY_FINGERPRINT \
  --gpg-homedir="$HOME/.config/cassini/secrets/flatpak-gnupg" \
  --legacy-bundle=/path/to/released/cassini-viewer.flatpak
```

The script requires both IDs, marks the old one as end-of-life, signs the
app/Locale refs and repository summary, and produces:

- `repo/`: the signed repository, containing the new ID and final old-ID rebase;
- `dist/cassini-talk.flatpakrepo`: the canonical URL and embedded public key;
- `dist/cassini-talk.flatpak`: the standalone bundle with `--repo-url` and public key;
- `dist/cassini-talk.gpg`: only the exported public key.

The private signing key stays in the explicit GnuPG home under
`~/.config/cassini/secrets`, outside repository/output directories. It is never
exported or committed. Local/development builds without `--release` remain
unsigned and do not point at the production update origin. Development builds
cannot use `--release`.

These commands **only prepare local artifacts**. Publishing waits for upload
access. The release owner uploads the signed repository and public repo/key
files to the canonical directory and attaches the signed bundle to the release;
this tooling performs no upload and creates no GitHub release or tag.

Flatpak records the previous ID when the user accepts the rebase. On the first
run it migrates `~/.var/app/com.gocassini.Viewer` to the new name and leaves a
compatibility link. That keeps existing absolute fallback Library paths
readable, along with settings, downloaded models and saved voices. The app
does not copy or merge private stores itself and gains no filesystem permission.

Once the repository is published, internal testers who installed the old
standalone bundle add the repository and accept the old-ID rebase. Do not
launch a separate fresh new-ID installation before rebasing the old one, or
delete the old app's data. The exact add/reinstall path is exercised by the
signed migration gate below.

The packaging gate tests a real local repository rebase from the pinned released
0.3.0 bundle, including an absolute Library path and model/voice data sentinels.
The native upgrade gate installs the actual released 0.3.0 Ubuntu package and
starts the replacement after installing both new Debian packages.

References: [Flatpak command reference](https://docs.flatpak.org/en/latest/flatpak-command-reference.html)
(`build-commit-from --end-of-life-rebase`) and
[Flatpak's data migration implementation](https://github.com/flatpak/flatpak/blob/1.17.4/common/flatpak-run.c).
