"""Unit tests for bridge.py's pure parts: python -m unittest discover -s tests"""

import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge as b  # noqa: E402

REPO = "example-org/examplecraft"
EXAMPLE = {
    "schema": 1,
    "id": "examplecraft",
    "name": "ExampleCraft",
    "tagline": "What it's for, in a few words",
    "repo": REPO,
    "publisher": "Example Org",
    "upstream": None,
    "bundle_id": "org.example.examplecraft",
    "msi_publisher": "Example Org",
    "apple_team": None,
    "website": "https://example.org/examplecraft",
    "color": "#7a45b8",
    "icon": "examplecraft-128.png",
}


def with_(field, value):
    d = dict(EXAMPLE)
    d[field] = value
    return json.dumps(d).encode()


def png(w, h, extra=0):
    return b"\x89PNG\r\n\x1a\n\0\0\0\x0dIHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\0" * extra


class Descriptor(unittest.TestCase):
    def test_example_reads_and_round_trips(self):
        d, p = b.parse_descriptor(json.dumps(EXAMPLE).encode(), REPO)
        self.assertEqual(p, [])
        d2, p2 = b.parse_descriptor(b.descriptor_json(d).encode(), "Example-Org/ExampleCraft")
        self.assertEqual(p2, [])
        self.assertEqual(d2["id"], "examplecraft")
        self.assertNotIn("apple_team", json.loads(b.descriptor_json(d)))

    def test_repo_must_match(self):
        _, p = b.parse_descriptor(json.dumps(EXAMPLE).encode(), "someone-else/examplecraft")
        self.assertTrue(any("doesn't match" in x for x in p), p)

    def test_artcraft_namespace_is_refused(self):
        for field, value in [
            ("id", "photocraft"),
            ("name", "WordCraft"),
            ("name", "DECKCRAFT"),
            ("bundle_id", "ai.storyteller.examplecraft"),
            ("bundle_id", "AI.Storyteller"),
            ("msi_publisher", " Learning Machines LLC "),
            ("apple_team", "DJ6XS33FX8"),
            ("publisher", "ArtCraft"),
            ("publisher", "storyteller.ai"),
        ]:
            d, p = b.parse_descriptor(with_(field, value), REPO)
            self.assertIsNone(d, f"{field}={value} accepted")
        d, _ = b.parse_descriptor(with_("repo", "storytold/examplecraft"), "storytold/examplecraft")
        self.assertIsNone(d)
        self.assertFalse(b.reserved_bundle("ai.storytellers.x"))

    def test_launcher_id_only_in_launcher_repo(self):
        self.assertFalse(b.reserved_id_for_repo("craft-unofficial-launcher", "yalkouassi/CRAFT-UNOFFICIAL-LAUNCHER"))
        self.assertTrue(b.reserved_id_for_repo("craft-unofficial-launcher", "someone/craft-unofficial-launcher"))

    def test_kelvin_sign_is_not_folded(self):
        # Rust's to_ascii_lowercase leaves U+212A alone, so this isn't "deckcraft".
        self.assertFalse(b.reserved_id("DECKCRAFT"))

    def test_hostile_fields_are_refused(self):
        for field, value in [
            ("schema", 2),
            ("schema", True),
            ("schema", 1.0),
            ("schema", "1"),
            ("id", "Example Craft"),
            ("id", "-x"),
            ("id", "a--b"),
            ("id", "a" * 41),
            ("id", None),
            ("name", "Example‮tfarC"),
            ("name", ""),
            ("name", " padded"),
            ("name", "tab\there"),
            ("name", "x" * 41),
            ("tagline", "x" * 81),
            ("website", "http://example.org"),
            ("website", "https://example.org/a|b"),
            ("website", "https://user@example.org/"),
            ("website", "https:///path"),
            ("website", "javascript:alert(1)"),
            ("website", "https://example.org/" + "a" * 190),
            ("color", "red"),
            ("color", "#12345g"),
            ("icon", "../icon.png"),
            ("icon", "icon.svg"),
            ("icon", ".hidden.png"),
            ("bundle_id", "examplecraft"),
            ("bundle_id", "org.1example.app"),
            ("bundle_id", "org..app"),
            ("apple_team", "short"),
            ("apple_team", "abcdefghij"),
            ("upstream", "not a repo"),
            ("upstream", "a/b.git"),
            ("former_ids", None),
            ("former_ids", ["photocraft"]),
            ("former_ids", ["a"] * 9),
            ("former_names", ["PhotoCraft"]),
            ("former_bundles", ["ai.storyteller.x"]),
        ]:
            d, p = b.parse_descriptor(with_(field, value), REPO)
            self.assertIsNone(d, f"{field}={value!r} accepted")
        self.assertIsNone(b.parse_descriptor(with_("surprise", True), REPO)[0], "unknown fields are refused")
        self.assertIsNone(b.parse_descriptor(b" " * (b.MAX_DESCRIPTOR_BYTES + 1), REPO)[0])
        self.assertIsNone(b.parse_descriptor(b"[]", REPO)[0])
        self.assertIsNone(b.parse_descriptor(b"\xef\xbb\xbf" + json.dumps(EXAMPLE).encode(), REPO)[0], "BOM")
        dup = json.dumps(EXAMPLE)[:-1] + ', "id": "other"}'
        self.assertIsNone(b.parse_descriptor(dup.encode(), REPO)[0], "duplicate keys")
        self.assertIsNone(b.parse_descriptor(json.dumps(EXAMPLE).replace('"Example Org"', '"\\ud800"', 1).encode(), REPO)[0])
        nan = json.dumps(EXAMPLE).replace('"schema": 1', '"schema": NaN')
        self.assertIsNone(b.parse_descriptor(nan.encode(), REPO)[0])
        self.assertIsNone(b.parse_descriptor(json.dumps(EXAMPLE).encode("utf-16"), REPO)[0])

    def test_every_problem_is_reported(self):
        d = dict(EXAMPLE, id="photocraft", color="blue")
        _, p = b.parse_descriptor(json.dumps(d).encode(), REPO)
        self.assertEqual(len(p), 2, p)

    def test_missing_optional_fields_are_fine(self):
        d = {k: v for k, v in EXAMPLE.items() if k not in ("upstream", "msi_publisher", "apple_team", "website", "icon")}
        self.assertEqual(b.parse_descriptor(json.dumps(d).encode(), REPO)[1], [])
        d.pop("color")
        self.assertIsNone(b.parse_descriptor(json.dumps(d).encode(), REPO)[0])

    def test_rust_whitespace_trim(self):
        self.assertFalse(b.valid_text("x　", 40))
        self.assertTrue(b.valid_text("Ünïcødé ✓", 40))
        self.assertTrue(b.valid_text("x" * 40, 40))
        self.assertTrue(b.valid_text("é" * 40, 40), "characters, not bytes")


class Contract(unittest.TestCase):
    GOOD = [
        "notecraft-1.2.0-windows-x64.msi",
        "notecraft-1.2.0-windows-x64-portable.zip",
        "notecraft-1.2.0-linux-x86_64.AppImage",
        "notecraft-1.2.0-macos-universal.dmg",
        "notecraft.png",
        "craft-app.json",
        "SHA256SUMS.txt",
    ]

    def desc(self, **kw):
        d = {"schema": 1, "id": "notecraft", "name": "NoteCraft", "tagline": "Notes", "repo": "o/notecraft", "publisher": "O",
             "bundle_id": "org.o.notecraft", "msi_publisher": "O", "color": "#112233", "icon": "notecraft.png"}
        d.update(kw)
        out, p = b.parse_descriptor(json.dumps(d).encode(), "o/notecraft")
        self.assertEqual(p, [])
        return out

    @staticmethod
    def sums(names):
        return "".join(f"{'0' * 64}  {n}\n" for n in names if n != b.SUMS_FILE)

    def test_good_release_passes(self):
        r = b.check_release(self.desc(), "v1.2.0", self.GOOD, self.sums(self.GOOD))
        self.assertTrue(r.ok, r.problems)
        self.assertEqual(len(r.packages), 4)
        self.assertEqual(r.packages[1].suffix, "-portable.zip")
        self.assertTrue(any("apple_team" in w for w in r.warnings))

    def test_missing_files_and_drift_fail(self):
        d = self.desc()
        self.assertFalse(b.check_release(d, "v1.2.0", [n for n in self.GOOD if n != "craft-app.json"], None).ok)
        self.assertFalse(b.check_release(d, "v1.3.0", self.GOOD, self.sums(self.GOOD)).ok)
        short = self.sums(self.GOOD[1:])
        r = b.check_release(d, "v1.2.0", self.GOOD, short)
        self.assertTrue(any("doesn't list notecraft-1.2.0-windows-x64.msi" in p for p in r.problems))
        extra = self.sums(self.GOOD) + f"{'0' * 64}  ghost.exe\n"
        self.assertFalse(b.check_release(d, "v1.2.0", self.GOOD, extra).ok)

    def test_misnamed_packages(self):
        assets = ["notecraft-setup.msi", "notecraft-1.2.0-amiga-68k.tar.gz", "craft-app.json", "SHA256SUMS.txt", "notecraft.png",
                  "notecraft-1.2.0-windows-x64.zip", "notecraft-cli-1.2.0-macos-universal.zip", "notes.txt"]
        r = b.check_release(self.desc(), "1.2.0", assets, None)
        self.assertTrue(any("notecraft-setup.msi" in p for p in r.problems), r.problems)
        self.assertTrue(any("amiga-68k" in p for p in r.problems), r.problems)
        self.assertTrue(any("notecraft-1.2.0-windows-x64.zip" in w for w in r.warnings), "plain .zip is ignored, not a package")

    def test_msi_needs_publisher(self):
        d = self.desc()
        d["msi_publisher"] = None
        r = b.check_release(d, "v1.2.0", self.GOOD, self.sums(self.GOOD))
        self.assertTrue(any("msi_publisher" in p for p in r.problems))

    def test_former_ids_are_stems(self):
        d = self.desc(former_ids=["oldnote"])
        self.assertIsNotNone(b.parse_package("oldnote-1.2.0-linux-x86_64.deb", b.stems(d)))

    def test_parse_package_semver(self):
        st = ["app"]
        self.assertEqual(b.parse_package("app-1.2.0-beta.1-linux-x86_64.rpm", st).version, "1.2.0-beta.1")
        self.assertEqual(b.parse_package("app-1.2.0+build.01-linux-x86_64.rpm", st).version, "1.2.0+build.01")
        for bad in ["app-v1.2.0-linux-x86_64.rpm", "app-01.2.0-linux-x86_64.rpm", "app-1.2-linux-x86_64.rpm",
                    "app-1.2.0-01-linux-x86_64.rpm", "app-1.2.0-linux-x86_64.exe", "app-1.2.0-linux-x86_64.Appimage",
                    "app-99999999999999999999.0.0-linux-x86_64.rpm", "apps-1.2.0-linux-x86_64.rpm"]:
            self.assertIsNone(b.parse_package(bad, st), bad)

    def test_sums_parsing(self):
        h = "a" * 64
        text = f"{h}  app-1.0.0-linux-x86_64.AppImage\r\n{h} *craft-app.json\nnot a line\n{h}  ../evil\n{'A' * 64}  x.txt\n{h}  a:b\n"
        got = b.parse_sha256sums(text)
        self.assertEqual([n for _, n in got], ["app-1.0.0-linux-x86_64.AppImage", "craft-app.json", "x.txt"])
        self.assertEqual(got[2][0], "a" * 64, "hex is lower-cased")

    def test_icon_header(self):
        self.assertEqual(b.check_icon(png(128, 128)), (128, 128))
        self.assertIsInstance(b.check_icon(png(4096, 128)), str)
        self.assertIsInstance(b.check_icon(png(0, 128)), str)
        self.assertIsInstance(b.check_icon(b"GIF89a"), str)
        self.assertIsInstance(b.check_icon(png(128, 128)[:18]), str)
        self.assertIsInstance(b.check_icon(png(128, 128, b.MAX_ICON_BYTES)), str)


def make_zip(path, entries, symlink=None):
    with zipfile.ZipFile(path, "w") as z:
        for name in entries:
            if name.endswith("/"):
                z.writestr(name, b"")
            else:
                z.writestr(name, b"data:" + name.encode())
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.create_system = 3
            info.external_attr = (0o120777) << 16
            z.writestr(info, b"/etc/passwd")


def make_tar(path, entries, links=()):
    with tarfile.open(path, "w:gz") as t:
        for name in entries:
            if name.endswith("/"):
                i = tarfile.TarInfo(name.rstrip("/"))
                i.type = tarfile.DIRTYPE
                t.addfile(i)
            else:
                data = b"bin:" + name.encode()
                i = tarfile.TarInfo(name)
                i.size, i.mode = len(data), 0o755
                t.addfile(i, io.BytesIO(data))
        for name, target in links:
            i = tarfile.TarInfo(name)
            i.type, i.linkname = tarfile.SYMTYPE, target
            t.addfile(i)


class Layouts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_zip_layouts(self):
        st = ["subcraft"]
        cases = {
            "flat": (["SubCraft.exe", "README.md"], True),
            "wrapped": (["w/", "w/subcraft.exe", "w/lib/x.dll"], True),
            "deep": (["w/x/subcraft.exe"], False),
            "two_roots": (["w/subcraft.exe", "other/readme"], False),
            "missing": (["w/other.exe"], False),
            "dup": (["a.txt", "A.TXT", "subcraft.exe"], False),
            "traversal": (["../subcraft.exe"], False),
        }
        for label, (entries, ok) in cases.items():
            p = self.d / f"{label}.zip"
            make_zip(p, entries)
            self.assertEqual(b.check_zip_layout(p, st) == [], ok, f"{label}: {b.check_zip_layout(p, st)}")
        p = self.d / "link.zip"
        make_zip(p, ["subcraft.exe"], symlink="evil")
        self.assertNotEqual(b.check_zip_layout(p, st), [])
        p = self.d / "backslash.zip"
        make_zip(p, ["w_subcraft.exe"])
        p.write_bytes(p.read_bytes().replace(b"w_subcraft.exe", b"w\\subcraft.exe"))
        self.assertNotEqual(b.check_zip_layout(p, st), [], "a backslash in an entry name is refused")
        p = self.d / "former.zip"
        make_zip(p, ["oldname.exe"])
        self.assertEqual(b.check_zip_layout(p, ["subcraft", "oldname"]), [])

    def test_tar_layouts(self):
        cases = {
            "bin": (["t/", "t/bin/", "t/bin/app", "t/share/x"], True),
            "flat_top": (["t/", "t/app", "t/README"], True),
            "flat_root": (["app", "README"], True),
            "deep": (["t/usr/bin/app"], False),
            "two_tops": (["a/bin/app", "b/readme"], False),
            "wrong_case": (["t/App"], False),
            "abs": (["/t/bin/app"], False),
        }
        for label, (entries, ok) in cases.items():
            p = self.d / f"{label}.tar.gz"
            make_tar(p, entries)
            self.assertEqual(b.check_tar_layout(p, "app") == [], ok, f"{label}: {b.check_tar_layout(p, 'app')}")
        p = self.d / "symbin.tar.gz"
        make_tar(p, ["t/", "t/real"], links=[("t/bin", "/usr/bin")])
        self.assertNotEqual(b.check_tar_layout(p, "app"), [])
        p = self.d / "symapp.tar.gz"
        make_tar(p, ["t/", "t/bin/"], links=[("t/bin/app", "/etc/passwd")])
        self.assertNotEqual(b.check_tar_layout(p, "app"), [])

    def test_repack_zip_moves_exe_to_root_and_keeps_bytes(self):
        src, dst = self.d / "in.zip", self.d / "out.zip"
        make_zip(src, ["App-1.0/x/", "App-1.0/x/subcraft.exe", "App-1.0/x/lib/a.dll"])
        self.assertNotEqual(b.check_zip_layout(src, ["subcraft"]), [])
        b.repack_zip(src, dst, ["subcraft"])
        self.assertEqual(b.check_zip_layout(dst, ["subcraft"]), [])
        with zipfile.ZipFile(dst) as z:
            self.assertEqual(sorted(z.namelist()), ["lib/a.dll", "subcraft.exe"])
            self.assertEqual(z.read("subcraft.exe"), b"data:App-1.0/x/subcraft.exe")

    def test_repack_zip_fixes_backslash_separators(self):
        src, dst = self.d / "in.zip", self.d / "out.zip"
        # Names written with `#` and patched to `\` afterwards (zipfile would turn `\` into `/` on
        # Windows); the contents don't contain the names, so the CRCs stay valid.
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("W#", b"")
            z.writestr("W#SubCraft.exe", b"program")
            z.writestr("W#third#x.txt", b"notes")
        src.write_bytes(src.read_bytes().replace(b"#", b"\\"))
        self.assertNotEqual(b.check_zip_layout(src, ["subcraft"]), [], "the launcher refuses backslashes")
        b.repack_zip(src, dst, ["subcraft"])
        self.assertEqual(b.check_zip_layout(dst, ["subcraft"]), [])
        with zipfile.ZipFile(dst) as z:
            self.assertEqual(sorted(i.orig_filename for i in z.infolist()), ["SubCraft.exe", "third/x.txt"])
            self.assertEqual(z.read("SubCraft.exe"), b"program")

    def test_repack_zip_refuses_to_drop_files(self):
        src = self.d / "in.zip"
        make_zip(src, ["a/b/subcraft.exe", "a/readme.txt"])
        with self.assertRaises(b.BridgeError):
            b.repack_zip(src, self.d / "out.zip", ["subcraft"])

    def test_repack_tar_to_bin_layout(self):
        src, dst = self.d / "in.tar.gz", self.d / "out.tar.gz"
        make_tar(src, ["T/", "T/app", "T/app-cli", "T/README", "T/third/x.txt"])
        b.repack_tar(src, dst, "app", "app-1.0.0-linux-x86_64")
        self.assertEqual(b.check_tar_layout(dst, "app"), [])
        with tarfile.open(dst) as t:
            names = {m.name for m in t.getmembers()}
            self.assertIn("app-1.0.0-linux-x86_64/bin/app", names)
            self.assertIn("app-1.0.0-linux-x86_64/bin/app-cli", names)
            self.assertIn("app-1.0.0-linux-x86_64/third/x.txt", names)
            m = t.getmember("app-1.0.0-linux-x86_64/bin/app")
            self.assertEqual(m.mode, 0o755)
            self.assertEqual(t.extractfile(m).read(), b"bin:T/app")

    def test_repack_tar_refuses_escaping_links(self):
        src = self.d / "in.tar.gz"
        make_tar(src, ["T/", "T/app"], links=[("T/evil", "../../etc/passwd")])
        with self.assertRaises(b.BridgeError):
            b.repack_tar(src, self.d / "o.tar.gz", "app", "x")


class Sums(unittest.TestCase):
    def test_sorted_and_without_itself_or_dotfiles(self):
        text = b.sums_text({"b.tar.gz": "2" * 64, "a.msi": "1" * 64, b.SUMS_FILE: "3" * 64, ".DS_Store": "4" * 64})
        self.assertEqual(text, f"{'1' * 64}  a.msi\n{'2' * 64}  b.tar.gz\n")

    def test_check_dir_catches_tampering(self):
        with tempfile.TemporaryDirectory() as t:
            dist = Path(t)
            desc = dict(EXAMPLE, id="app", name="App", icon="app.png", msi_publisher=None, website=None)
            (dist / "craft-app.json").write_text(b.descriptor_json(desc))
            (dist / "app.png").write_bytes(png(64, 64))
            make_tar(dist / "app-1.0.0-linux-x86_64.tar.gz", ["app-1.0.0/", "app-1.0.0/bin/", "app-1.0.0/bin/app"])
            (dist / "LICENSE").write_text("license")
            b.write_sums(dist)
            r = b.check_dir(dist, REPO, "v1.0.0")
            self.assertTrue(r.ok, r.problems)
            self.assertEqual(len(r.packages), 1)
            (dist / "app.png").write_bytes(png(64, 65))
            r = b.check_dir(dist, REPO, "v1.0.0")
            self.assertTrue(any("app.png: its SHA-256" in p for p in r.problems), r.problems)
            b.write_sums(dist)
            self.assertFalse(b.check_dir(dist, REPO, "v1.0.1").ok, "tag drift")
            self.assertFalse(b.check_dir(dist, "other/repo", "v1.0.0").ok, "repo drift")
            make_tar(dist / "app-1.0.0-linux-x86_64.tar.gz", ["x/y/app"])
            b.write_sums(dist)
            r = b.check_dir(dist, REPO, "v1.0.0")
            self.assertTrue(any("no regular file" in p for p in r.problems), r.problems)


class Planning(unittest.TestCase):
    CFG = """
[bridge]
upstream = "opengeos/RustGIS"
repo = "YalKouassi/rustgis-for-unofficial-launcher"
skip = ['\\.whl$', '-setup\\.exe$', '\\.zsync$']

[descriptor]
id = "rustgis"
name = "RustGIS"
tagline = "GIS"
publisher = "Open Geospatial Solutions"
bundle_id = "org.rustgis.RustGIS"
color = "#d36736"

[[rename]]
match = 'RustGIS-(?P<version>[^-]+)-windows-x86_64-portable\\.zip'
to = '{id}-{version}-windows-x64-portable.zip'

[[rename]]
match = 'rustgis_(?P<version>[^_]+)_(?P<arch>amd64|arm64)\\.deb'
to = '{id}-{version}-linux-{arch}.deb'
map = { arch = { amd64 = "x86_64", arm64 = "aarch64" } }
"""

    def cfg(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "bridge.toml"
            p.write_text(self.CFG)
            return b.load_config(p)

    def test_rename_rules(self):
        c = self.cfg()
        self.assertEqual(b.plan_asset(c, "RustGIS-0.4.0-windows-x86_64-portable.zip", "rustgis", "0.4.0")[1], "rustgis-0.4.0-windows-x64-portable.zip")
        self.assertEqual(b.plan_asset(c, "rustgis_0.4.0_arm64.deb", "rustgis", "0.4.0")[1], "rustgis-0.4.0-linux-aarch64.deb")
        self.assertEqual(b.plan_asset(c, "rustgis-0.4.0-cp312-abi3-win_amd64.whl", "rustgis", "0.4.0")[0], "skip")
        self.assertEqual(b.plan_asset(c, "RustGIS-0.4.0-windows-x86_64-setup.exe", "rustgis", "0.4.0")[0], "skip")
        self.assertEqual(b.plan_asset(c, "something-else.tar.gz", "rustgis", "0.4.0")[0], "unmatched")
        kind, _, _, why = b.plan_asset(c, "RustGIS-0.3.9-windows-x86_64-portable.zip", "rustgis", "0.4.0")
        self.assertEqual(kind, "skip")
        self.assertIn("isn't the tag's", why)
        # Matching is whole-name: a prefix match doesn't count.
        self.assertEqual(b.plan_asset(c, "xRustGIS-0.4.0-windows-x86_64-portable.zip.bak", "rustgis", "0.4.0")[0], "unmatched")

    def test_descriptor_is_filled_in(self):
        d = b.build_descriptor(self.cfg())
        self.assertEqual(d["repo"], "YalKouassi/rustgis-for-unofficial-launcher")
        self.assertEqual(d["upstream"], "opengeos/RustGIS")
        self.assertEqual(b.check_descriptor(d, d["repo"]), [])

    def test_bad_config_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "bridge.toml"
            p.write_text(self.CFG.replace('[descriptor]', '[descriptor]\nrepo = "x/y"'))
            with self.assertRaises(b.ConfigError):
                b.load_config(p)

    def test_upstream_adoption_stops_the_bridge(self):
        c = self.cfg()
        valid = dict(EXAMPLE, repo="opengeos/RustGIS", id="rustgis", name="RustGIS", icon=None)
        release = {"tag_name": "v1.0.0", "assets": [{"name": "craft-app.json", "browser_download_url": "https://github.com/x"}]}
        real = b.fetch_bytes
        try:
            b.fetch_bytes = lambda *a, **k: json.dumps(valid).encode()
            self.assertTrue(b.upstream_adopted(c, release, None))
            b.fetch_bytes = lambda *a, **k: json.dumps(dict(valid, repo="someone/else")).encode()
            self.assertFalse(b.upstream_adopted(c, release, None), "a descriptor for another repo doesn't count")
            self.assertFalse(b.upstream_adopted(c, {"tag_name": "v1", "assets": []}, None))
        finally:
            b.fetch_bytes = real

    def test_notes_fence_cannot_be_closed(self):
        body = "@someone fixed owner/repo#12\n````\nescape"
        f = b.fence(body)
        self.assertTrue(f.startswith("`````text"))
        self.assertTrue(f.endswith("`````"))


if __name__ == "__main__":
    unittest.main()
