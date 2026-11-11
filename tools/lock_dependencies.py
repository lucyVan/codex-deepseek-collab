"""Lock already-resolved Windows CPython 3.12 wheels, without downloading/installing."""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import re
import zipfile


def normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def lock(wheels: Path, output: Path, evidence: Path):
    distributions = {}
    for path in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(path) as archive:
            candidates = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(candidates) != 1:
                raise ValueError("wheel must have one distribution metadata file")
            metadata = BytesParser().parsebytes(archive.read(candidates[0]))
        name, version = normalized(metadata["Name"]), metadata["Version"]
        if name in distributions:
            raise ValueError("duplicate distribution wheel")
        distributions[name] = {"version": version, "wheel": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    if not distributions:
        raise ValueError("no wheels found")
    direct = output.with_name("requirements.in").read_text(encoding="utf-8")
    for line in direct.splitlines():
        if not line or line.startswith("#"):
            continue
        name, version = line.split("==")
        if distributions.get(normalized(name), {}).get("version") != version:
            raise ValueError("direct requirement missing or different")
    text = "# Windows x64 / CPython 3.12. Generated from verified wheel artifacts.\n# Install with --require-hashes --only-binary=:all:. No floating requirements.\n"
    text += "\n".join(f"{name}=={item['version']} --hash=sha256:{item['sha256']}" for name, item in sorted(distributions.items()))+"\n"
    output.write_text(text, encoding="utf-8", newline="\n")
    evidence.write_text(json.dumps({"platform": "win_amd64", "python": "3.12", "distributions": distributions, "lock_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"locked_distributions": len(distributions), "lock": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheels", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    lock(args.wheels, args.output, args.evidence)
