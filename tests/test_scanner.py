"""Scanner identity and matching (without touching the system)."""
import os
import shutil
import tempfile
import unittest

from rshub.engine.removal import scanner
from rshub.engine.removal.scanner import Identity, strip_version


class Names(unittest.TestCase):
    def test_strip_version(self):
        self.assertEqual(strip_version("7-Zip 26.05 (x64)"), "7-Zip")
        self.assertEqual(strip_version("Ollama version 0.40.14"), "Ollama")
        self.assertEqual(strip_version("Microsoft 365 Apps for enterprise - en-us"), "Microsoft 365 Apps for enterprise")
        self.assertEqual(strip_version("Notepad++ (64-bit x64)"), "Notepad++")
        self.assertEqual(strip_version("FabFilter Pro-Q 4"), "FabFilter Pro-Q 4")  # the 4 is the generation
        self.assertEqual(strip_version("Stremio versión 5.0.26"), "Stremio")

    def test_product_without_vendor(self):
        ident = Identity({"id": "a", "name": "Xfer Records Serum 2", "publisher": "Xfer Records"})
        self.assertIsNotNone(ident.match("Serum2.vst3"))
        self.assertIsNotNone(ident.match("Serum 2", in_vendor=True))

    def test_shared_tokens_between_versions_are_dropped(self):
        a = {"id": "a", "name": "FabFilter Pro-C 2", "publisher": "FabFilter"}
        b = {"id": "b", "name": "FabFilter Pro-C 3", "publisher": "FabFilter"}
        ident = Identity(a, [a, b])
        self.assertIsNone(ident.match("Pro-C", in_vendor=True))     # shared with Pro-C 3
        self.assertIsNotNone(ident.match("Pro-C 2", in_vendor=True))  # its own

    def test_short_generic_names_penalized(self):
        ident = Identity({"id": "a", "name": "Notepad++ (64-bit x64)", "publisher": "Notepad++ Team"})
        pts, _ = ident.match("Notepad")
        self.assertLess(pts, 70)

    def test_vendor_detection(self):
        ident = Identity({"id": "a", "name": "iZotope RX 11 Audio Editor", "publisher": "iZotope, Inc."})
        self.assertTrue(ident.is_vendor("iZotope"))
        self.assertFalse(ident.is_vendor("Native Instruments"))


class Locations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mrs_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_refs_location_boundaries(self):
        loc = os.path.join(self.tmp, "Foo")
        os.makedirs(loc)
        ident = Identity({"id": "a", "name": "Foo App", "location": loc})
        self.assertTrue(ident.refs_location(f'"{loc}\\foo.exe" --tray'))
        self.assertTrue(ident.refs_location(loc))
        self.assertFalse(ident.refs_location(loc + "Bar\\x.exe"))  # FooBar is not Foo

    def test_a_sibling_folder_with_a_longer_name_is_not_ours(self):
        # "Microsoft VS Code" must not claim the shortcuts, PATH entries or services of "Microsoft VS Code Insiders"
        loc = os.path.join(self.tmp, "Foo")
        os.makedirs(os.path.join(self.tmp, "Foo Bar", "bin"))
        os.makedirs(loc)
        ident = Identity({"id": "a", "name": "Foo App", "location": loc})
        self.assertFalse(ident.refs_location(os.path.join(self.tmp, "Foo Bar", "bin")))
        self.assertFalse(ident.refs_location(f'"{os.path.join(self.tmp, "Foo Bar", "foo.exe")}" --tray'))
        self.assertFalse(ident.refs_location(os.path.join(self.tmp, "Foo Bar")))
        self.assertTrue(ident.refs_location(f"{loc} --tray"))     # an unquoted argument still counts
        self.assertTrue(ident.refs_location(f'"{loc}\\foo.exe" --tray'))

    def test_nested_program_references_go_to_the_deepest(self):
        steam = os.path.join(self.tmp, "Steam")
        game = os.path.join(steam, "steamapps", "common", "game")
        os.makedirs(game)
        s = {"id": "s", "name": "Steam", "location": steam}
        g = {"id": "g", "name": "Some Game", "location": game}
        ident = Identity(s, [s, g])
        self.assertTrue(ident.refs_location(os.path.join(steam, "steam.exe")))
        self.assertFalse(ident.refs_location(os.path.join(game, "game.exe")))  # it belongs to the game, not to Steam

    def test_vendor_install_location_is_narrowed(self):
        epic = os.path.join(self.tmp, "Epic Games")
        os.makedirs(os.path.join(epic, "Launcher"))
        os.makedirs(os.path.join(epic, "Fortnite"))
        ident = Identity({"id": "e", "name": "Epic Games Launcher", "publisher": "Epic Games, Inc.", "location": epic})
        self.assertEqual([os.path.basename(x) for x in ident.location_paths], ["Launcher"])

    def test_game_library_detection(self):
        os.makedirs(os.path.join(self.tmp, "lib", "steamapps"))
        self.assertTrue(scanner._is_game_library(os.path.join(self.tmp, "lib")))
        self.assertFalse(scanner._is_game_library(self.tmp))


if __name__ == "__main__":
    unittest.main()
