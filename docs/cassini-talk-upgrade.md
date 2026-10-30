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

Publish the replacement ref in the same repository as the old ref before
marking the old ref as end-of-life. With both refs present locally, run:

```sh
sh build-aux/flatpak/rename-ref.sh repo
# Separate development repository/ref, if distributed:
sh build-aux/flatpak/rename-ref.sh devel-repo --devel
```

Then sign and publish that repository through its normal release process.
The helper only changes a local repository. It refuses to mark an old ref
unless the matching replacement exists. It does not publish anything.

Flatpak records the previous ID when the user accepts the rebase. On the first
run it migrates `~/.var/app/com.gocassini.Viewer` to the new name and leaves a
compatibility link. That keeps existing absolute fallback Library paths
readable, along with settings, downloaded models and saved voices. The app
does not copy or merge private stores itself and gains no filesystem permission.

**Release dependency:** the existing GitHub release distributes a standalone
bundle, without a Cassini update repository. Installing a new-ID bundle beside
it does not establish a previous-ID migration. Before distributing this rename,
the release owner must provide the common repository and an explicit rebase
path for those bundle users. Do not advise deleting the old app's data.

The packaging gate tests a real local repository rebase from the pinned released
0.3.0 bundle, including an absolute Library path and model/voice data sentinels.
The native upgrade gate installs the actual released 0.3.0 Ubuntu package and
starts the replacement after installing both new Debian packages.

References: [Flatpak command reference](https://docs.flatpak.org/en/latest/flatpak-command-reference.html)
(`build-commit-from --end-of-life-rebase`) and
[Flatpak's data migration implementation](https://github.com/flatpak/flatpak/blob/1.17.4/common/flatpak-run.c).
