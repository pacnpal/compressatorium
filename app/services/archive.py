import os
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple
import shutil

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

        return entries

    def _list_zip(self, archive_path: str) -> List[dict]:
        """List contents of a ZIP file."""
        entries = []
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
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
        with py7zr.SevenZipFile(archive_path, "r") as zf:
            archive_info = zf.archiveinfo()
            if hasattr(archive_info, "files"):
                for name, info in archive_info.files.items():
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
            # Fallback for different py7zr versions
            if not entries:
                for entry in zf.list():
                    if not entry.is_directory:
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
        with rarfile.RarFile(archive_path, "r") as rf:
            for info in rf.infolist():
                if info.is_dir():
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
        """
        Extract a specific file from an archive.

        Returns:
            Tuple of (extracted_file_path, temp_directory)
        """
        ext = Path(archive_path).suffix.lower()
        temp_dir = tempfile.mkdtemp(prefix="chd_extract_")

        try:
            if ext == ".zip":
                extracted = self._extract_from_zip(archive_path, internal_path, temp_dir)
            elif ext == ".7z" and HAS_7Z:
                extracted = self._extract_from_7z(archive_path, internal_path, temp_dir)
            elif ext == ".rar" and HAS_RAR:
                extracted = self._extract_from_rar(archive_path, internal_path, temp_dir)
            else:
                raise ValueError(f"Unsupported archive format: {ext}")

            return extracted, temp_dir

        except Exception:
            # Clean up on error
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    def _extract_from_zip(self, archive_path: str, internal_path: str, temp_dir: str) -> str:
        """Extract a file from ZIP."""
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extract(internal_path, temp_dir)
        return os.path.join(temp_dir, internal_path)

    def _extract_from_7z(self, archive_path: str, internal_path: str, temp_dir: str) -> str:
        """Extract a file from 7z."""
        with py7zr.SevenZipFile(archive_path, "r") as zf:
            zf.extract(temp_dir, [internal_path])
        return os.path.join(temp_dir, internal_path)

    def _extract_from_rar(self, archive_path: str, internal_path: str, temp_dir: str) -> str:
        """Extract a file from RAR."""
        with rarfile.RarFile(archive_path, "r") as rf:
            rf.extract(internal_path, temp_dir)
        return os.path.join(temp_dir, internal_path)

    def cleanup_temp_dir(self, temp_dir: str):
        """Clean up a temporary extraction directory."""
        shutil.rmtree(temp_dir, ignore_errors=True)


archive_service = ArchiveService()
