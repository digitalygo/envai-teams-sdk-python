import argparse
import email
import re
import stat
from pathlib import Path, PurePosixPath
import tarfile
import zipfile


def validate_paths(names: list[str], allowed_roots: set[str] | None = None) -> None:
    seen = set()
    for name in names:
        parts = PurePosixPath(name).parts
        normalized = str(PurePosixPath(name))
        if allowed_roots is not None and (not parts or parts[0] not in allowed_roots):
            raise ValueError("Package archive contains an unexpected package root")
        if normalized in seen:
            raise ValueError("Package archive contains duplicate normalized paths")
        seen.add(normalized)
        if name.startswith("/") or ".." in parts or "\\" in name or re.match(r"^[A-Za-z]:", name):
            raise ValueError("Package archive contains an unsafe path")
        if any(part in {".github", ".git", "node_modules", "release-assets"} or part.startswith(".env") for part in parts):
            raise ValueError("Package archive contains development tooling or environment files")


def validate(directory: Path, version: str) -> None:
    files = list(directory.iterdir())
    expected = {
        f"envai_teams_sdk-{version}-py3-none-any.whl",
        f"envai_teams_sdk-{version}.tar.gz",
    }
    if {path.name for path in files} != expected:
        raise ValueError("Expected exactly this release's wheel and source distribution")
    for path in files:
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as archive:
                if any(stat.S_ISLNK(info.external_attr >> 16) for info in archive.infolist()):
                    raise ValueError("Package archive contains a symbolic link")
                validate_paths(archive.namelist(), {"envai", f"envai_teams_sdk-{version}.dist-info"})
                names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
                if len(names) != 1:
                    raise ValueError("Wheel must have exactly one package metadata file")
                metadata = archive.read(names[0]).decode()
        else:
            with tarfile.open(path) as archive:
                if any(not (member.isfile() or member.isdir()) for member in archive.getmembers()):
                    raise ValueError("Package archive contains a link or special file")
                validate_paths(archive.getnames(), {f"envai_teams_sdk-{version}"})
                members = [member for member in archive.getmembers() if member.name.count("/") == 1 and member.name.endswith("/PKG-INFO")]
                if len(members) != 1:
                    raise ValueError("Source distribution must have exactly one package metadata file")
                metadata = archive.extractfile(members[0]).read().decode()
        parsed = email.message_from_string(metadata)
        if parsed["Name"] != "envai-teams-sdk" or parsed["Version"] != version:
            raise ValueError("Package identity or version does not match this release")
    print(f"Validated envai-teams-sdk {version} wheel and source distribution")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("version")
    args = parser.parse_args()
    validate(args.directory, args.version)
