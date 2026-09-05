"""Keep the README's local presentation usable on GitHub and in the package."""

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent.parent


class PresentationHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []
        self.details_depth = 0
        self.minimum_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.images.append(dict(attrs))
        if tag == "details":
            self.details_depth += 1

    def handle_endtag(self, tag):
        if tag == "details":
            self.details_depth -= 1
            self.minimum_depth = min(self.minimum_depth, self.details_depth)


class ReadmePresentationTest(unittest.TestCase):
    def setUp(self):
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.html = PresentationHTML()
        self.html.feed(self.readme)

    def test_local_readme_images_exist_and_are_explicitly_packaged(self):
        files = json.loads((ROOT / "package.json").read_text())["files"]
        self.assertTrue(self.html.images, "the README has no cover image")
        for image in self.html.images:
            with self.subTest(image=image.get("src")):
                source = image["src"]
                self.assertNotIn(":", source)
                self.assertFalse(Path(source).is_absolute())
                self.assertNotIn("..", Path(source).parts)
                self.assertTrue((ROOT / source).is_file())
                self.assertIn(source, files)
                self.assertTrue(image.get("alt", "").strip())

    def test_cover_is_self_contained_accessible_svg_without_active_content(self):
        for image in self.html.images:
            with self.subTest(image=image["src"]):
                root = ET.parse(ROOT / image["src"]).getroot()
                self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
                self.assertIsNotNone(root.find("{http://www.w3.org/2000/svg}title"))
                self.assertIsNotNone(root.find("{http://www.w3.org/2000/svg}desc"))
                self.assertIn("viewBox", root.attrib)
                for element in root.iter():
                    tag = element.tag.rsplit("}", 1)[-1]
                    self.assertNotIn(tag, ("script", "foreignObject", "image", "style"))
                    for key, value in element.attrib.items():
                        key = key.rsplit("}", 1)[-1]
                        self.assertFalse(key.lower().startswith("on"))
                        if key == "href":
                            self.assertTrue(value.startswith("#"))
                        for url in re.findall(r"url\(([^)]+)\)", value):
                            self.assertTrue(url.startswith("#"))

    def test_landing_navigation_has_real_heading_targets(self):
        landing = self.readme.split("<details>", 1)[0]
        headings = set()
        for heading in re.findall(r"^#{1,6} (.+)$", self.readme, re.M):
            slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
            headings.add(slug)
        for target in re.findall(r"\]\(#([^)]+)\)", landing):
            self.assertIn(target, headings)

    def test_collapsible_reference_does_not_hide_the_remaining_guide(self):
        self.assertEqual(self.html.details_depth, 0)
        self.assertEqual(self.html.minimum_depth, 0)
        reference, rest = self.readme.split("</details>", 1)
        self.assertIn("### How identity is preserved", reference)
        for heading in ("## Many peers", "## Install", "## Update", "## Commands", "## Limits"):
            self.assertIn(heading, rest)


if __name__ == "__main__":
    unittest.main()
