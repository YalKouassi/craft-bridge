# Making an app installable by Craft Unofficial Launcher

This page is for **app authors**, and is written to be handed to a coding agent as-is ("make my
releases follow JOINING.md"). It is complete on its own: you don't need anything else to follow it.

Craft Unofficial Launcher lists, installs and updates unofficial Craft-style apps. It knows nothing
about an app in advance. Each app describes itself in a small file, `craft-app.json`, attached to
every GitHub release, and each release follows a fixed contract (file names and checksums). The
launcher treats all of it as untrusted input and refuses a release that breaks a rule. It never
guesses.

Once your releases follow this page, users can add your app by pasting your repo's URL into the
launcher's **Add app** box, and it can be listed in the launcher's default index (see
[Getting listed](#getting-listed)). No fork or bridge repo is needed.

## Checklist

Every item below is a hard requirement unless it says otherwise.

1. `craft-app.json` is committed at your repo root and valid ([The descriptor](#the-descriptor)).
2. Every release is a normal published GitHub release: not a draft, not a prerelease. The launcher
   reads your **latest** release.
3. The tag is the version, with or without a leading `v`: `v1.2.0` or `1.2.0`.
4. The release attaches `craft-app.json`, unchanged from the repo.
5. The release attaches at least one package named
   `<id>-<version>-<os>-<arch>[-portable].<ext>` ([Package names](#package-names)), and
   `<version>` equals the tag without its `v`.
6. Archives have the layout the launcher installs from ([Archive layouts](#archive-layouts)).
7. The release attaches `SHA256SUMS.txt` listing every package, `craft-app.json` and the icon, and
   nothing that isn't attached ([Checksums](#checksums)).
8. If the release ships an `.msi`, `craft-app.json` names `msi_publisher`, and it equals the
   Publisher (Manufacturer) your MSI registers. The MSI's product name equals the descriptor's `name`.
9. If `craft-app.json` names an `icon`, that PNG is attached: at most 256 KB and 512 px a side.
10. Nothing claims to be an official ArtCraft app ([Reserved names](#reserved-names)).
11. The release folder passes the checker before you upload it ([Checking](#checking-a-release)).

Optional:

- `apple_team`: without it the launcher won't install your `.dmg` (it only opens a copy that's
  already installed).
- `website`, `web_demo`, `former_ids`, `former_names`, `former_bundles`.

## The descriptor

```json
{
  "schema": 1,
  "id": "examplecraft",
  "name": "ExampleCraft",
  "tagline": "What it's for, in a few words",
  "repo": "example-org/examplecraft",
  "publisher": "Example Org",
  "upstream": null,
  "bundle_id": "org.example.examplecraft",
  "msi_publisher": "Example Org",
  "apple_team": null,
  "website": "https://example.org/examplecraft",
  "web_demo": "https://example.org/examplecraft/try",
  "color": "#7a45b8",
  "icon": "examplecraft-128.png"
}
```

- UTF-8 JSON, at most 16 KB.
- **Unknown fields are refused**: a typo is an error.
- Optional fields may be left out or set to `null`.
- The `former_*` lists may be left out or set to `[]`, but not `null`.
- "Plain text" below means:
  - the stated number of characters;
  - no leading or trailing spaces;
  - no control characters;
  - no bidirectional-override characters (U+200E, U+200F, U+202A–U+202E, U+2066–U+2069).

| Field | Required | Rule |
|---|---|---|
| `schema` | yes | `1` |
| `id` | yes | 1–40 chars of `a-z`, `0-9`, `-`. No leading or trailing `-`, no `--`. It is also the stem of package names and the executable's name. |
| `name` | yes | Display name, plain text, 1–40 chars. Must not contain `/ \ : * ? " < > \|` or start or end with `.`. |
| `tagline` | yes | Plain text, 1–80 chars. |
| `repo` | yes | `owner/name` of the repo the releases are published in. It must be the repo the launcher reads it from. |
| `publisher` | yes | Who makes it, as shown on the app's card. Plain text, 1–60 chars. |
| `upstream` | no | Only for bridge repos: the `owner/name` being mirrored. Leave `null` in your own repo. |
| `bundle_id` | yes | Reverse-DNS id (`org.example.examplecraft`): at least two dot-separated parts of ASCII letters, digits, `-`, `_`, none starting with a digit. Used as the macOS bundle id and the Flatpak/desktop-file id. |
| `msi_publisher` | if you ship an `.msi` | The Publisher your MSI registers in Windows' installed-apps list, exactly. Plain text, 1–100 chars. |
| `apple_team` | no | Your Apple Developer ID Team ID (10 chars of `A-Z0-9`) that signs the `.app` in your `.dmg`. |
| `website` | no | An `https://` link, at most 200 bytes. |
| `web_demo` | no | A page where the app can be tried in a web browser without installing it; the launcher shows it as a "Web demo" link. Same rules as `website`. |
| `color` | yes | Brand colour, `#rrggbb`. |
| `icon` | no | File name of a `.png` attached to every release, e.g. `examplecraft-128.png`. |
| `former_ids` | no | Up to 8 older ids; packages named with one are still accepted. |
| `former_names` | no | Up to 8 older display names your installers registered. |
| `former_bundles` | no | Up to 8 older bundle ids. |

The launcher **pins** `id`, `bundle_id`, `msi_publisher` and `apple_team` the first time it sees
your app. A later release that changes one is refused and the user keeps the old version. To rename,
keep the old value in the matching `former_*` list.

## Reserved names

These identify official ArtCraft apps and are refused (case-insensitive):

- **`id`, `name`, `former_ids`, `former_names`:** `photocraft`, `vectorcraft`, `filmcraft`,
  `lightcraft`, `pdfcraft`, `printcraft`, `effectcraft`, `designcraft`, `figcraft`, `wordcraft`,
  `soundcraft`, `deckcraft`, `cadcraft`, `gridcraft`, `artcraft`, `artcraft-launcher`,
  `craft-launcher`, `craft-unofficial-launcher`.
- **`bundle_id`:** anything under `ai.storyteller`.
- **`publisher` and `msi_publisher`:** `ArtCraft`, `Storyteller`, `Storyteller.ai`,
  `Learning Machines`, `Learning Machines LLC`.
- **`apple_team`:** `DJ6XS33FX8`.
- **Repo owner:** `storytold`.

## Package names

```text
<id>-<version>-<os>-<arch>[-portable].<ext>
```

- `<version>` is SemVer starting with a digit (`1.2.0`, `1.2.0-beta.1`), and equals the tag without
  its `v`.
- `<os>-<arch>` is one of:
  - **Windows:** `windows-x64`, `windows-x86`, `windows-arm64`
  - **macOS:** `macos-universal`, `macos-x86_64`, `macos-aarch64`, `macos-arm64`
  - **Linux:** `linux-x86_64`, `linux-aarch64`, `linux-riscv64`
  - **FreeBSD:** `freebsd-x86_64`, `freebsd-aarch64`
- The ending is one of these (case-sensitive): `-portable.zip`, `.tar.gz`, `.msi`, `.dmg`,
  `.AppImage`, `.deb`, `.rpm`, `.pkg.tar.zst`, `.flatpak`. A Windows zip must end in
  `-portable.zip`.

Examples:

```text
examplecraft-1.2.0-windows-x64.msi
examplecraft-1.2.0-windows-x64-portable.zip
examplecraft-1.2.0-macos-universal.dmg
examplecraft-1.2.0-linux-x86_64.AppImage
examplecraft-1.2.0-linux-x86_64.tar.gz
examplecraft-1.2.0-linux-x86_64.deb
```

The launcher picks the file for the user's computer from these names alone, before downloading
anything.

- A file that starts with your id and ends in a package ending but breaks the pattern is an error,
  for example `examplecraft-setup.msi`.
- Any other file is ignored with a warning: release notes, `.zsync`, CLI builds, source archives
  and so on. You can keep attaching them.

You don't have to rename your existing files. Upload the same file under both names, or only under
the contract name.

## Archive layouts

- **Use `/` as the folder separator.** An archive entry that uses `\` is refused as unsafe. This is
  common with zips made by PowerShell's `Compress-Archive` on older versions. Use
  `tar -a -c -f x.zip ...`, 7-Zip, or Python's `zipfile` instead.
- **Portable zip** (`-portable.zip`): `<id>.exe` (any letter case) is at the root of the zip, or at
  the root of a single top-level folder that holds everything.
- **Tarball** (`.tar.gz`): the binary is at `bin/<id>` or `<id>`, optionally inside a single
  top-level folder. It must be a regular file, not a link.
- **No escaping paths.** Absolute paths, `..`, and links pointing outside the archive are refused.
- **Installers** (MSI, DMG, AppImage, deb, rpm, Flatpak) are used as they are.

## Checksums

`SHA256SUMS.txt` is plain `sha256sum` output, one `<64 hex>  <file name>` line per file. It must:

- list every package, `craft-app.json` and the icon;
- list nothing that isn't attached;
- have every checksum right. The launcher verifies each download against it.

Listing extra attached files is fine. Write it last, after every other file is in the folder.

## Checking a release

Gather exactly the files the release will attach into one folder (say `dist/`), then run this
repo's checker. It needs Python 3.11 or newer and nothing else:

```sh
curl -fsSLO https://raw.githubusercontent.com/YalKouassi/craft-bridge/main/bridge.py
python -I bridge.py --sums dist                                         # writes dist/SHA256SUMS.txt
python -I bridge.py --check dist --repo example-org/examplecraft --tag v1.2.0
```

- **Exit `0`:** the release keeps the contract. Warnings may still be printed.
- **Exit `1`:** it breaks the contract, and every problem is listed.
- **Exit `2`:** the checker couldn't run.

It applies the launcher's rules, including archive layouts. In GitHub Actions, run it after building
and before uploading:

```yaml
- name: Check the release against the craft-app.json contract
  run: |
    curl -fsSLO https://raw.githubusercontent.com/YalKouassi/craft-bridge/<commit-sha>/bridge.py
    cp craft-app.json dist/
    python -I bridge.py --sums dist
    python -I bridge.py --check dist --repo "${{ github.repository }}" --tag "${{ github.ref_name }}"
```

Pin `<commit-sha>` to a commit of this repo rather than `main`.

## Getting listed

Once a release passes the checker:

- **Anyone can use it right away.** Paste `https://github.com/<owner>/<repo>` into the launcher's
  **Add app** box.
- **To be in the default list**, open an issue in
  [YalKouassi/craft-bridge](https://github.com/YalKouassi/craft-bridge/issues/new?template=add-app.yml)
  with your repo URL. Being listed vouches for nothing: the launcher shows every unofficial app with
  a notice that ArtCraft and the launcher's maintainers don't review or support it.

If your app is currently mirrored by a bridge repo, the bridge notices your first release that
attaches a valid `craft-app.json`. From then on it stops publishing, the index is pointed at your
repo, and the bridge is retired.

## Notes for coding agents

- Make the smallest change that satisfies the checklist. Usually that means:
  - adding `craft-app.json`;
  - an extra rename and copy step in the release workflow;
  - writing `SHA256SUMS.txt`;
  - running the checker.

  Don't restructure the build.
- Read `msi_publisher`, the MSI's product name and `bundle_id` from the project's existing
  packaging: the WiX or `cargo-wix` file, `Cargo.toml` `[package.metadata.*]`, the Flatpak
  manifest, the `.desktop` file, `Info.plist`. Don't invent them. If the project ships no MSI, leave
  `msi_publisher` out.
- Never set `apple_team` unless the project really signs its macOS app with that Developer ID team.
- Keep every existing release asset; add the contract-named files alongside them if users or other
  tools rely on the old names.
- Run the checker locally on a dry-run `dist/` folder and paste its output into the PR.
