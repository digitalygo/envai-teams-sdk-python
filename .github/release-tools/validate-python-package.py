import argparse
import email
from pathlib import Path
import tarfile
import zipfile


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
                names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
                if len(names) != 1:
                    raise ValueError("Wheel must have exactly one package metadata file")
                metadata = archive.read(names[0]).decode()
        else:
            with tarfile.open(path) as archive:
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
