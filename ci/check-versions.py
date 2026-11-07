#!/usr/bin/env python3
"""Refuse a version mismatch before anything is built (handbook Part 4, Ch34; as Ch17).

The version is written in three files: pyproject.toml, src/beamline_tango/__init__.py
(__version__) and debian/changelog (the source package, so both .debs). In a tag
pipeline the tag (vMAJOR.MINOR.PATCH) must say the same. Reads files only; never
rewrites them - a release bumps all three in one commit, and this check refuses the
tag if one was missed.
"""

import os
import re
import sys
import tomllib
from pathlib import Path

root = Path(__file__).resolve().parent.parent
found = {}

with open(root / "pyproject.toml", "rb") as f:
    found["pyproject.toml"] = tomllib.load(f)["project"]["version"]

init = root / "src/beamline_tango/__init__.py"
m = re.search(r'^__version__ = "([^"]+)"$', init.read_text(), re.MULTILINE)
if not m:
    sys.exit(f'refusing: no __version__ = "..." line in {init.relative_to(root)}')
found["src/beamline_tango/__init__.py"] = m.group(1)

first = (root / "debian/changelog").read_text().splitlines()[0]
m = re.match(r"^\S+ \(([^)]+)\) ", first)
if not m:
    sys.exit(f"refusing: no version in debian/changelog's first line: {first!r}")
# A native package has no Debian revision; strip one (-N) if it is ever added.
found["debian/changelog"] = re.sub(r"-[^-]+$", "", m.group(1))

tag = os.environ.get("CI_COMMIT_TAG", "")
if tag:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        sys.exit(f"refusing: tag {tag!r} is not vMAJOR.MINOR.PATCH")
    found[f"tag {tag}"] = tag[1:]

for source, version in found.items():
    print(f"{version:<12} {source}")
if len(set(found.values())) != 1:
    sys.exit("refusing: the versions differ")
print(f"versions agree: {found['pyproject.toml']}")
