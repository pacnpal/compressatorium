import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import List, Tuple

try:
    import py7zr
    HAS_7Z = True
except ImportError:
    HAS_7Z = False

try:
    import rarfile
    HAS_RAR = True
except ImportError:
    HAS_RAR = False


ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar"}
CONVERTIBLE_EXTENSIONS = {".gdi", ".iso", ".cue", ".bin"}


class ArchiveService:
    """Service for handling compressed archives."""

    def __init__(self):
        self._temp_dirs: dict = {}

    def is_archive(self, filename: str) -> bool:
        """Check if a file is a supported archive."""
        ext = Path(filename).suffix.lower()
        return ext in ARCHIVE_EXTENSIONS

    def list_archive_contents(self, archive_path: str) -> List[dict]:
        """List convertible files inside an archive."""
        ext = Path(archive_path).suffix.lower()
        entries = []

        try:
            if ext == ".zip":
                entries = self._list_zip(archive_path)
            elif ext == ".7z" and HAS_7Z:
                entries = self._list_7z(archive_path)
            elif ext == ".rar" and HAS_RAR:
                entries = self._list_rar(archive_path)
        except Exception as e:
            print(f"Error listing archive {archive_path}: {e}")

        for entry in entries:
            entry["output_stem"] = self._output_stem_for_member(entry["internal_path"])

        return entries

    def _list_zip(self, archive_path: str) -> List[dict]:
        """List contents of a ZIP file."""
        entries = []
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                try:
                    self._validate_member(info.filename)
                except ValueError:
                    continue
                ext = Path(info.filename).suffix.lower()
                if ext in CONVERTIBLE_EXTENSIONS:
                    entries.append({
                        "archive_path": archive_path,
                        "internal_path": info.filename,
                        "name": os.path.basename(info.filename),
                        "size": info.file_size,
                        "extension": ext,
                        "convertible": True
                    })
        return entries

    def _list_7z(self, archive_path: str) -> List[dict]:
        """List contents of a 7z file."""
        entries = []
        with py7zr.SevenZipFile(archive_path, "r") as zf:  # type: ignore[name-defined]
            archive_info = zf.archiveinfo()
            if hasattr(archive_info, "files"):
                for name, info in archive_info.files.items():
                    try:
                        self._validate_member(name)
                    except ValueError:
                        continue
                    ext = Path(name).suffix.lower()
                    if ext in CONVERTIBLE_EXTENSIONS:
                        entries.append({
                            "archive_path": archive_path,
                            "internal_path": name,
                            "name": os.path.basename(name),
                            "size": getattr(info, 'uncompressed', 0),
                            "extension": ext,
                            "convertible": True
                        })
            if not entries:
                for entry in zf.list():
                    if entry.is_directory:
                        continue
                    try:
                        self._validate_member(entry.filename)
                    except ValueError:
                        continue
                    ext = Path(entry.filename).suffix.lower()
                    if ext in CONVERTIBLE_EXTENSIONS:
                        entries.append({
                            "archive_path": archive_path,
                            "internal_path": entry.filename,
                            "name": os.path.basename(entry.filename),
                            "size": entry.uncompressed,
                            "extension": ext,
                            "convertible": True
                        })
        return entries

    def _list_rar(self, archive_path: str) -> List[dict]:
        """List contents of a RAR file."""
        entries = []
        with rarfile.RarFile(archive_path, "r") as rf:  # type: ignore[name-defined]
            for info in rf.infolist():
                if info.is_dir():
                    continue
                try:
                    self._validate_member(info.filename)
                except ValueError:
                    continue
                ext = Path(info.filename).suffix.lower()
                if ext in CONVERTIBLE_EXTENSIONS:
                    entries.append({
                        "archive_path": archive_path,
                        "internal_path": info.filename,
                        "name": os.path.basename(info.filename),
                        "size": info.file_size,
                        "extension": ext,
                        "convertible": True
                    })
        return entries

    def extract_file(self, archive_path: str, internal_path: str) -> Tuple[str, str]:
        """Extract a specific file from an archive into a temp directory."""
        ext = Path(archive_path).suffix.lower()
        temp_dir = tempfile.mkdtemp(prefix="chd_extract_")

        try:
            destination = self._prepare_destination(temp_dir, internal_path)

            if ext == ".zip":
                extracted = self._extract_from_zip(archive_path, internal_path, destination)
            elif ext == ".7z" and HAS_7Z:
                extracted = self._extract_from_7z(archive_path, internal_path, destination)
            elif ext == ".rar" and HAS_RAR:
                extracted = self._extract_from_rar(archive_path, internal_path, destination)
            else:
                raise ValueError(f"Unsupported archive format: {ext}")

            return extracted, temp_dir

        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    def _validate_member(self, member: str) -> List[str]:
        """Ensure an archive member path cannot escape the extraction directory."""
        if "\\" in member:
            raise ValueError("Backslashes are not allowed in archive members")
        normalized = member.strip()
        if normalized.startswith('/'):
            raise ValueError("Absolute paths are not allowed inside archives")
        segments = normalized.split('/')
        if not segments or any(part in ('', '.', '..') for part in segments):
            raise ValueError("Invalid archive member path")
        if any(':' in part for part in segments):
            raise ValueError("Drive letters are not allowed inside archives")
        return segments

    def _prepare_destination(self, temp_dir: str, member: str) -> str:
        segments = self._validate_member(member)
        base = Path(temp_dir).resolve()
        candidate = base.joinpath(*segments).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise ValueError("Archive member resolves outside temp directory") from exc
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return str(candidate)

    def _extract_from_zip(self, archive_path: str, internal_path: str, destination: str) -> str:
        with zipfile.ZipFile(archive_path, "r") as zf:
            try:
                with zf.open(internal_path) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except KeyError as exc:
                raise FileNotFoundError(f"{internal_path} not found in archive") from exc
        return destination

    def extract_related_files(self, archive_path: str, internal_path: str, temp_dir: str):
        """Extract sibling files for multi-file formats like .cue or .gdi."""
        entry_ext = Path(internal_path).suffix.lower()
        if entry_ext not in {".cue", ".gdi"}:
            return

        parent = PurePosixPath(internal_path).parent
        if str(parent) == ".":
            parent = PurePosixPath("")

        archive_ext = Path(archive_path).suffix.lower()
        if archive_ext == ".zip":
            self._extract_related_from_zip(archive_path, parent, temp_dir)
        elif archive_ext == ".7z" and HAS_7Z:
            self._extract_related_from_7z(archive_path, parent, temp_dir)
        elif archive_ext == ".rar" and HAS_RAR:
            self._extract_related_from_rar(archive_path, parent, temp_dir)
        else:
            raise ValueError(f"Unsupported archive format: {archive_ext}")

    def _extract_related_from_zip(self, archive_path: str, parent: PurePosixPath, temp_dir: str):
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                try:
                    self._validate_member(info.filename)
                except ValueError:
                    continue
                if not self._is_same_parent(info.filename, parent):
                    continue
                destination = self._prepare_destination(temp_dir, info.filename)
                with zf.open(info.filename) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    def _extract_related_from_7z(self, archive_path: str, parent: PurePosixPath, temp_dir: str):
        with py7zr.SevenZipFile(archive_path, "r") as zf:  # type: ignore[name-defined]
            targets = []
            for entry in zf.list():
                if entry.is_directory:
                    continue
                name = entry.filename
                try:
                    self._validate_member(name)
                except ValueError:
                    continue
                if self._is_same_parent(name, parent):
                    targets.append(name)
            if targets:
                zf.extract(targets=targets, path=temp_dir)

    def _extract_related_from_rar(self, archive_path: str, parent: PurePosixPath, temp_dir: str):
        with rarfile.RarFile(archive_path, "r") as rf:  # type: ignore[name-defined]
            for info in rf.infolist():
                if info.is_dir():
                    continue
                try:
                    self._validate_member(info.filename)
                except ValueError:
                    continue
                if not self._is_same_parent(info.filename, parent):
                    continue
                destination = self._prepare_destination(temp_dir, info.filename)
                with rf.open(info.filename) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    @staticmethod
    def _is_same_parent(member: str, parent: PurePosixPath) -> bool:
        member_parent = PurePosixPath(member).parent
        if str(parent) == "":
            return str(member_parent) in ("", ".")
        return member_parent == parent

    @staticmethod
    def _output_stem_for_member(member: str) -> str:
        member_path = PurePosixPath(member)
        parent = member_path.parent
        stem = member_path.stem
        if str(parent) in ("", "."):
            return stem
        safe_parent = "_".join([p for p in parent.parts if p not in ("", ".")])
        return f"{safe_parent}_{stem}"

    def _extract_from_7z(self, archive_path: str, internal_path: str, destination: str) -> str:
        with py7zr.SevenZipFile(archive_path, "r") as zf:  # type: ignore[name-defined]
            try:
                data = zf.read([internal_path])
            except Exception as exc:
                raise FileNotFoundError(f"{internal_path} not found in archive") from exc

            if internal_path not in data:
                raise FileNotFoundError(f"{internal_path} not found in archive")

            with open(destination, "wb") as dst:
                shutil.copyfileobj(data[internal_path], dst)
        return destination

    def _extract_from_rar(self, archive_path: str, internal_path: str, destination: str) -> str:
        with rarfile.RarFile(archive_path, "r") as rf:  # type: ignore[name-defined]
            try:
                with rf.open(internal_path) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except (rarfile.Error, KeyError) as exc:  # type: ignore[name-defined]
                raise FileNotFoundError(f"{internal_path} not found in archive") from exc
        return destination

    def cleanup_temp_dir(self, temp_dir: str):
        """Clean up a temporary extraction directory."""
        shutil.rmtree(temp_dir, ignore_errors=True)


archive_service = ArchiveService()
