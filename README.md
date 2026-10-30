# craft-bridge

Bridges bring apps whose authors haven't adopted the `craft-app.json` release contract into
[Craft Unofficial Launcher](https://github.com/YalKouassi/craft-unofficial-launcher).

A **bridge** is a small public repo (`<id>-for-unofficial-launcher`) that watches one upstream
project. When the upstream project publishes a release, the bridge mirrors it: it downloads the
builds upstream published, renames them to the launcher's naming contract, adds a
`craft-app.json`, the icon, upstream's licence files and `SHA256SUMS.txt`, checks the result
against the launcher's rules, and publishes it as a release of the bridge repo. The launcher
lists the bridge repo like any other app.

This repo holds the tool every bridge runs: `bridge.py`, a reusable GitHub Actions workflow, and
a template.

Bridges are unofficial. They aren't made, reviewed or endorsed by the upstream projects they
mirror, or by ArtCraft.

## Trust

Installing an app through a bridge means trusting **the bridge's maintainer** as well as the
upstream project: the launcher checks a bridge's releases against the bridge's own
`SHA256SUMS.txt`, which the bridge writes.

What a bridge does, and doesn't do, to keep that trust small:

- **It never builds anything.** Every package is a file upstream published on its own GitHub
  release, downloaded from `github.com/<upstream>/releases/download/...` only.
- **It verifies before mirroring.** Each download must match the size and SHA-256 digest GitHub
  reports for the upstream asset and, when upstream publishes a checksum file
  (`upstream_sums`), the checksum listed there. Any mismatch aborts the run and nothing is
  published.
- **It never changes a program file.** Packages are renamed. An archive (a portable `.zip` or a
  `.tar.gz`) is **repacked** only when its folder layout would stop the launcher installing it
  (for example, the executable sitting two folders deep). Repacking moves entries between folders
  and recompresses them; the bytes of every file inside are copied unchanged, and nothing is
  dropped (`bridge.py` refuses to repack if a file would be lost). Each release's notes list every
  file, the upstream asset it came from, its SHA-256, and whether it was repacked.
- **Installers stay upstream's.** MSI, DMG, AppImage, deb, rpm and Flatpak files are never
  opened, only renamed.
- **Everything downloaded is treated as untrusted data.** Archives are only listed or copied
  entry by entry (never extracted to disk, never run), with size and entry-count caps, and
  entries with absolute paths, `..`, or links that escape the archive are refused.
- **Upstream's licences travel with the builds.** Every release attaches upstream's `LICENSE*`
  and `NOTICE*` files from the same tag, and the run fails if upstream has no licence file.

To audit a bridge release, compare the hashes in its notes or `SHA256SUMS.txt` with upstream's
release (the "Upstream asset" column names the file).

## When upstream adopts the contract

If an upstream release ever attaches its own valid `craft-app.json` (one whose `repo` is the
upstream repo), the bridge has done its job. `bridge.py` then **publishes nothing**, prints

```text
NOTICE: <upstream> now follows the procedure (...); point the index at https://github.com/<upstream> and retire this bridge.
```

and exits 0. The launcher index should then list the upstream repo instead of the bridge, and the
bridge repo can be archived. (An upstream `craft-app.json` that isn't valid is reported as a
warning, and the bridge keeps mirroring.)

## Adding a bridge

1. Inspect the upstream release: asset names, what's inside the archives (the Windows
   executable's name and folder, the Linux binary's path), the MSI's `Manufacturer` and
   `ProductName`, and the app's reverse-DNS id (Flatpak manifest, `.desktop` file, macOS
   `Info.plist`). Treat downloads as untrusted: read archives, never run them.
2. Create a public repo `<owner>/<id>-for-unofficial-launcher` with:
   - `bridge.toml` (start from [`template/bridge.toml`](template/bridge.toml));
   - `.github/workflows/bridge.yml` (copy
     [`template/.github/workflows/bridge.yml`](template/.github/workflows/bridge.yml)); it runs
     every 30 minutes and on demand;
   - a README saying it's an unofficial mirror, who maintains it, and linking upstream;
   - licence files for the bridge's own files.
3. Dry-run it locally and check the result with the launcher's own checker:

   ```sh
   python -I ../craft-bridge/bridge.py --dry-run          # builds into ./dist/<tag>
   craft-release-check dir dist/<tag> --repo <owner>/<id>-for-unofficial-launcher --tag <tag>
   ```

4. Push, run the workflow once (Actions → bridge → Run workflow), and confirm with
   `craft-release-check github --repo <owner>/<id>-for-unofficial-launcher`.
5. Add `https://github.com/<owner>/<id>-for-unofficial-launcher` to the launcher's app index.

GitHub disables scheduled workflows in a public repo after 60 days without commits; re-enable it
from the Actions tab (or run it by hand) if that happens.

## `bridge.toml`

```toml
[bridge]
upstream = "opengeos/RustGIS"                          # the project being mirrored
repo = "YalKouassi/rustgis-for-unofficial-launcher"   # this bridge (must be where it runs)
upstream_sums = ["SHA256SUMS"]   # upstream checksum files to verify downloads against (optional)
skip = ['\.whl$', '-setup\.exe$']  # regexes (searched) for upstream assets not to mirror
# licenses = ["LICENSE"]         # default "auto": every LICENSE*/COPYING*/NOTICE* at the repo root
# strict = true                  # fail when an upstream asset matches no rule (default: skip it)
# max_asset_mb = 1024            # per-asset download cap
# notes_chars = 4000             # how much of upstream's release notes to quote

[descriptor]          # craft-app.json; schema, repo, upstream and icon are filled in
id = "rustgis"
name = "RustGIS"
tagline = "A fast, free and open-source GIS in pure Rust"
publisher = "Open Geospatial Solutions"
bundle_id = "org.rustgis.RustGIS"
msi_publisher = "Open Geospatial Solutions"   # only when the release ships an .msi
# apple_team = "..."                          # only if you can prove upstream's Developer ID team
website = "https://rustgis.app"
color = "#d36736"
# former_ids / former_names / former_bundles as in the spec

[icon]                # a PNG of at most 512 px a side and 256 KB, saved as <id>.png
url = "https://raw.githubusercontent.com/{upstream}/{tag}/path/icon-256.png"
# or take it from inside a mirrored upstream archive:
# archive = 'RustGIS-{version}-linux-x86_64\.tar\.gz'
# member = 'RustGIS-{version}-linux-x86_64/rustgis\.png'

[[rename]]            # first rule whose regex matches the whole asset name wins
match = 'RustGIS-(?P<version>[0-9][^-]*)-windows-x86_64-portable\.zip'
to = '{id}-{version}-windows-x64-portable.zip'
# repack = "auto"     # auto: only if the launcher couldn't install it; "always"; "never"

[[rename]]
match = 'rustgis_(?P<version>[^_]+)_(?P<arch>amd64|arm64)\.deb'
to = '{id}-{version}-linux-{arch}.deb'
map = { arch = { amd64 = "x86_64", arm64 = "aarch64" } }
```

In `to`, `{id}` and `{version}` (the tag without its leading `v`) are always available, along with
every named group of `match`, after `map`. If `match` captures `version` and it differs from the
tag's version, the asset is skipped. `tools/icon.py` (needs Pillow) shrinks an icon that's too
big.

## `bridge.py`

Python 3.11 or newer, standard library only.

```text
python -I bridge.py [--config bridge.toml] [--out dist] [--dry-run] [--force] [--tag TAG] [--backfill N]
python -I bridge.py --check DIR --tag TAG
```

- With no flags, it mirrors upstream's latest release (not drafts or prereleases), and exits 0
  without doing anything if the bridge already has a release with that tag.
- `--dry-run` builds and checks into `--out/<tag>` and publishes nothing.
- `--force` rebuilds and replaces an existing bridge release.
- `--tag` mirrors a specific upstream tag.
- `--backfill N` also mirrors up to N older upstream releases the bridge lacks; only the newest
  is marked as the bridge's latest release.
- `--check` validates a folder as a release of this bridge.

Exit codes: `0` done or nothing to do, `1` the release couldn't be built or breaks the contract,
`2` a config, argument or network problem. `GITHUB_TOKEN` (or `GH_TOKEN`) is sent to
`api.github.com` only, never to download hosts, and is passed to `gh` for publishing.

Before publishing, `bridge.py` checks the folder with a Python port of the launcher's
`crates/descriptor` rules (the descriptor's fields, the reserved ArtCraft namespace, package
names and platform tokens, version against the tag, `SHA256SUMS.txt` completeness and every
checksum, `msi_publisher` when there's an MSI, and the icon's PNG header and size) plus the
launcher's install rules for portable zips and tarballs. If the two ever disagree, the launcher's
Rust code wins; run `craft-release-check` to be sure. It publishes through `gh release create` as
a draft first, uploads everything, and then makes the release public, so an interrupted run never
leaves a half-uploaded public release.

Tests: `python -m unittest discover -s tests`.

## The reusable workflow

`.github/workflows/bridge.yml` is called by each bridge's workflow:

```yaml
permissions:
  contents: write
jobs:
  bridge:
    uses: YalKouassi/craft-bridge/.github/workflows/bridge.yml@main
    with:
      tag: ''          # optional
      force: false     # optional
      # backfill: 3
      # bridge-ref: <commit>   # pin the bridge.py version (default main)
```

It checks out the bridge repo and this repo (at `bridge-ref`), sets up Python 3.12 and runs
`bridge.py` with the job's `GITHUB_TOKEN`.

## Licence

`MIT OR Apache-2.0`, at your option: [LICENSE-MIT](LICENSE-MIT), [LICENSE-APACHE](LICENSE-APACHE).
This licence covers this repo's own files only; mirrored apps keep their upstream licences.
