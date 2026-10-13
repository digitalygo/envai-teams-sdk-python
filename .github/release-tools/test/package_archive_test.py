import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("package_check", Path(__file__).parents[1] / "validate-python-package.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PackagePathsTest(unittest.TestCase):
    def test_package_sources_are_allowed(self):
        module.validate_paths(["envai_teams_sdk-1.0.0a2/src/envai/client.py", "envai_teams_sdk-1.0.0a2/PKG-INFO", "envai/_contracts/openapi.json"])

    def test_development_files_and_unsafe_paths_are_rejected(self):
        for path in ["package/.github/workflows/release.yml", "package/.github/release-tools/node_modules/example/index.js", "package/node_modules/example/index.js", "package/.git/config", "package/release-assets/old.whl", "package/.env", "package/.env.production", "../escape", "/absolute", "C:/escape", "package\\..\\escape"]:
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    module.validate_paths([path])

    def test_duplicate_normalized_paths_are_rejected(self):
        with self.assertRaises(ValueError):
            module.validate_paths(["package/src/client.py", "package/src/./client.py"])

    def test_unexpected_package_roots_are_rejected(self):
        with self.assertRaises(ValueError):
            module.validate_paths(["other_sdk/client.py"], {"envai", "envai_teams_sdk-1.0.0a2.dist-info"})

class FullArchivesTest(unittest.TestCase):
    def test_links_paths_duplicates_and_roots(self):
        import contextlib
        import io
        import tarfile
        import tempfile
        import warnings
        import zipfile

        version = "1.0.0a2"
        root = f"envai_teams_sdk-{version}"
        metadata = f"Metadata-Version: 2.4\nName: envai-teams-sdk\nVersion: {version}\n".encode()
        cases = ["normal", "tar_symlink", "tar_hardlink", "tar_fifo", "zip_symlink", "tar_backslash", "zip_backslash", "tar_drive_absolute", "zip_drive_absolute", "tar_duplicate", "zip_duplicate", "tar_wrong_root", "zip_wrong_root"]
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                target = Path(directory)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    with zipfile.ZipFile(target / f"{root}-py3-none-any.whl", "w") as archive:
                        archive.writestr(root + ".dist-info/METADATA", metadata)
                        if case == "zip_symlink":
                            info = zipfile.ZipInfo("envai/link")
                            info.create_system = 3
                            info.external_attr = 0o120777 << 16
                            archive.writestr(info, "../../outside")
                        elif case == "zip_backslash":
                            archive.writestr("..\\outside", "bad")
                        elif case == "zip_drive_absolute":
                            archive.writestr("C:/outside", "bad")
                        elif case == "zip_wrong_root":
                            archive.writestr("other_sdk/client.py", "bad")
                        else:
                            archive.writestr("envai/client.py", "pass")
                            if case == "zip_duplicate":
                                archive.writestr("envai/client.py", "different")
                with tarfile.open(target / f"{root}.tar.gz", "w:gz") as archive:
                    def add(name, data=b"pass", kind=tarfile.REGTYPE):
                        info = tarfile.TarInfo(name)
                        info.type = kind
                        if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                            info.linkname = "../../outside"
                            archive.addfile(info)
                        elif kind == tarfile.REGTYPE:
                            info.size = len(data)
                            archive.addfile(info, io.BytesIO(data))
                        else:
                            archive.addfile(info)
                    add(root + "/PKG-INFO", metadata)
                    if case == "tar_symlink":
                        add(root + "/src/envai/link", kind=tarfile.SYMTYPE)
                    elif case == "tar_hardlink":
                        add(root + "/src/envai/link", kind=tarfile.LNKTYPE)
                    elif case == "tar_fifo":
                        add(root + "/src/envai/pipe", kind=tarfile.FIFOTYPE)
                    elif case == "tar_backslash":
                        add(root + "/..\\outside")
                    elif case == "tar_drive_absolute":
                        add("C:/outside")
                    elif case == "tar_wrong_root":
                        add("other_sdk/private.py")
                    else:
                        add(root + "/src/envai/client.py")
                        if case == "tar_duplicate":
                            add(root + "/src/envai/client.py", b"different")
                with contextlib.redirect_stdout(io.StringIO()):
                    if case == "normal":
                        module.validate(target, version)
                    else:
                        with self.assertRaises(ValueError):
                            module.validate(target, version)


if __name__ == "__main__":
    unittest.main()
