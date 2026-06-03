import os
import shutil
import tempfile
import zipfile
import time
import logging
from logging_setup import get_logger
from pathlib import Path, PurePosixPath
from typing import List, Tuple, Optional, Union, Dict

from config import settings
from utils.junk import is_junk_path

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
# Fallback set of convertible archive-member extensions, used only when the
# tool registry can't be consulted. The authoritative list comes from
# ``ArchiveService._convertible_extensions()`` (which unions every mode that
# allows archive input). Historically this was hardcoded to CHDMAN's sources,
# which silently hid 3DS members (.3ds/.cci/.cia) inside archives, issue #113.
CONVERTIBLE_EXTENSIONS = {
    ".gdi", ".iso", ".cue", ".bin",          # chdman create modes
    ".gcz", ".wia", ".rvz", ".wbfs",         # Dolphin (.iso shared above)
    ".cci", ".cia", ".3ds",                  # 3DS (z3ds compress)
}

logger = get_logger("archive")


class ArchiveService:
    """Service for handling compressed archives."""

    def __init__(self):
        self._temp_dirs: dict = {}

    @staticmethod
    def _archive_limits() -> Tuple[int, int, int]:
        max_entries = max(0, int(getattr(settings, "archive_max_entries", 0) or 0))
        max_member_size = max(
            0, int(getattr(settings, "archive_max_member_size", 0) or 0)
        )
        max_total_size = max(
            0, int(getattr(settings, "archive_max_total_size", 0) or 0)
        )
        return max_entries, max_member_size, max_total_size

    @staticmethod
    def _format_size(size: int) -> str:
        return f"{size} bytes"

    @staticmethod
    def _coerce_size(value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        try:
            size = int(value)
        except (TypeError, ValueError):
            return None
        if size < 0:
            return None
        return size

    def _check_member_size(self, size: int, *, member: str) -> None:
        _, max_member_size, _ = self._archive_limits()
        if max_member_size > 0 and size > max_member_size:
            raise ValueError(
                f"Archive member exceeds max size: {member} ({self._format_size(size)})"
            )

    def _check_total_size(self, total_size: int) -> None:
        _, _, max_total_size = self._archive_limits()
        if max_total_size > 0 and total_size > max_total_size:
            raise ValueError(
                f"Archive extraction exceeds max total size ({self._format_size(total_size)})"
            )

    def _size_limits_enabled(self) -> bool:
        _, max_member_size, max_total_size = self._archive_limits()
        return max_member_size > 0 or max_total_size > 0

    def _create_temp_dir(self) -> str:
        base_dir = settings.temp_dir
        if base_dir is None:
            base_dir = str(Path(settings.data_dir) / "temp")
        if base_dir:
            try:
                Path(base_dir).mkdir(parents=True, exist_ok=True)
                return tempfile.mkdtemp(prefix="chd_extract_", dir=base_dir)
            except OSError as exc:
                logger.warning("Failed to create temp dir %s: %s", base_dir, exc)
        return tempfile.mkdtemp(prefix="chd_extract_")

    def is_archive(self, filename: str) -> bool:
        """Check if a file is a supported archive."""
        ext = Path(filename).suffix.lower()
        return ext in ARCHIVE_EXTENSIONS

    @staticmethod
    def _convertible_extensions() -> frozenset:
        """Extensions treated as convertible archive members.

        Sourced from the tool registry so archive listings surface the same
        inputs the conversion path can actually accept from an archive
        (chdman create + 3DS today). Falls back to the static set if the
        registry can't be imported (defensive, it always loads in-app).
        """
        try:
            from services.tools import registry

            exts = registry.archive_input_extensions()
            if exts:
                return exts
        except Exception:  # pragma: no cover - registry always loads in-app
            logger.debug(
                "Tool registry unavailable; using static convertible extensions",
                exc_info=True,
            )
        return frozenset(CONVERTIBLE_EXTENSIONS)

    def list_archive_contents(
        self, archive_path: str, *, include_meta: bool = False
    ) -> Union[List[dict], Dict[str, Union[List[dict], bool]]]:
        """List convertible files inside an archive."""
        ext = Path(archive_path).suffix.lower()
        entries = []
        truncated = False
        start = time.monotonic()

        try:
            if ext == ".zip":
                entries, truncated = self._list_zip(archive_path)
            elif ext == ".7z" and HAS_7Z:
                entries, truncated = self._list_7z(archive_path)
            elif ext == ".rar" and HAS_RAR:
                entries, truncated = self._list_rar(archive_path)
        except Exception as e:
            logger.exception("Error listing archive %s: %s", archive_path, e)
        finally:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Listed archive %s entries=%d in %.2fs",
                    archive_path,
                    len(entries),
                    time.monotonic() - start,
                )

        entries = self._filter_preferred_entries(entries)

        for entry in entries:
            entry["output_stem"] = self._output_stem_for_member(entry["internal_path"])

        if include_meta:
            return {"entries": entries, "truncated": truncated}
        return entries

    @staticmethod
    def _filter_preferred_entries(entries: List[dict]) -> List[dict]:
        if not entries:
            return entries

        exts_by_parent = {}
        for entry in entries:
            parent = PurePosixPath(entry["internal_path"]).parent
            exts_by_parent.setdefault(parent, set()).add(entry.get("extension"))

        filtered = []
        for entry in entries:
            ext = entry.get("extension")
            parent = PurePosixPath(entry["internal_path"]).parent
            exts = exts_by_parent.get(parent, set())
            if ext == ".bin" and (".cue" in exts or ".gdi" in exts):
                continue
            filtered.append(entry)

        return filtered

    def _list_zip(self, archive_path: str) -> Tuple[List[dict], bool]:
        """List contents of a ZIP file."""
        entries = []
        max_entries, max_member_size, max_total_size = self._archive_limits()
        convertible = self._convertible_extensions()
        total_size = 0
        truncated = False
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if is_junk_path(info.filename):
                    continue
                try:
                    self._validate_member(info.filename)
                except ValueError:
                    continue
                ext = Path(info.filename).suffix.lower()
                if ext in convertible:
                    if max_member_size > 0 and info.file_size > max_member_size:
                        logger.warning(
                            "Skipping oversized archive member %s (%s)",
                            info.filename,
                            self._format_size(info.file_size),
                        )
                        truncated = True
                        continue
                    if max_total_size > 0 and (total_size + info.file_size) > max_total_size:
                        logger.warning(
                            "Archive %s hit max total size limit (%s)",
                            archive_path,
                            self._format_size(max_total_size),
                        )
                        truncated = True
                        break
                    total_size += info.file_size
                    entries.append(
                        {
                            "archive_path": archive_path,
                            "internal_path": info.filename,
                            "name": os.path.basename(info.filename),
                            "size": info.file_size,
                            "extension": ext,
                            "convertible": True,
                        }
                    )
                    if max_entries > 0 and len(entries) >= max_entries:
                        logger.warning(
                            "Archive %s hit max entry limit (%s)",
                            archive_path,
                            max_entries,
                        )
                        truncated = True
                        break
        return entries, truncated

    def _list_7z(self, archive_path: str) -> Tuple[List[dict], bool]:
        """List contents of a 7z file."""
        entries = []
        max_entries, max_member_size, max_total_size = self._archive_limits()
        convertible = self._convertible_extensions()
        total_size = 0
        truncated = False
        with py7zr.SevenZipFile(archive_path, "r") as zf:  # type: ignore[name-defined]
            archive_info = zf.archiveinfo()
            if hasattr(archive_info, "files"):
                for name, info in archive_info.files.items():
                    if is_junk_path(name):
                        continue
                    try:
                        self._validate_member(name)
                    except ValueError:
                        continue
                    ext = Path(name).suffix.lower()
                    if ext in convertible:
                        size = self._coerce_size(getattr(info, "uncompressed", None))
                        if size is None:
                            if max_member_size > 0 or max_total_size > 0:
                                logger.warning(
                                    "Skipping archive member %s (unknown size)",
                                    name,
                                )
                                truncated = True
                                continue
                            size = 0
                        if max_member_size > 0 and size > max_member_size:
                            logger.warning(
                                "Skipping oversized archive member %s (%s)",
                                name,
                                self._format_size(size),
                            )
                            truncated = True
                            continue
                        if max_total_size > 0 and (total_size + size) > max_total_size:
                            logger.warning(
                                "Archive %s hit max total size limit (%s)",
                                archive_path,
                                self._format_size(max_total_size),
                            )
                            truncated = True
                            break
                        total_size += size
                        entries.append(
                            {
                                "archive_path": archive_path,
                                "internal_path": name,
                                "name": os.path.basename(name),
                                "size": size,
                                "extension": ext,
                                "convertible": True,
                            }
                        )
                        if max_entries > 0 and len(entries) >= max_entries:
                            logger.warning(
                                "Archive %s hit max entry limit (%s)",
                                archive_path,
                                max_entries,
                            )
                            truncated = True
                            break
                if truncated:
                    return entries, truncated
            if not entries:
                for entry in zf.list():
                    if entry.is_directory:
                        continue
                    if is_junk_path(entry.filename):
                        continue
                    try:
                        self._validate_member(entry.filename)
                    except ValueError:
                        continue
                    ext = Path(entry.filename).suffix.lower()
                    if ext in convertible:
                        size = self._coerce_size(entry.uncompressed)
                        if size is None:
                            if max_member_size > 0 or max_total_size > 0:
                                logger.warning(
                                    "Skipping archive member %s (unknown size)",
                                    entry.filename,
                                )
                                truncated = True
                                continue
                            size = 0
                        if max_member_size > 0 and size > max_member_size:
                            logger.warning(
                                "Skipping oversized archive member %s (%s)",
                                entry.filename,
                                self._format_size(size),
                            )
                            truncated = True
                            continue
                        if max_total_size > 0 and (total_size + size) > max_total_size:
                            logger.warning(
                                "Archive %s hit max total size limit (%s)",
                                archive_path,
                                self._format_size(max_total_size),
                            )
                            truncated = True
                            break
                        total_size += size
                        entries.append(
                            {
                                "archive_path": archive_path,
                                "internal_path": entry.filename,
                                "name": os.path.basename(entry.filename),
                                "size": size,
                                "extension": ext,
                                "convertible": True,
                            }
                        )
                        if max_entries > 0 and len(entries) >= max_entries:
                            logger.warning(
                                "Archive %s hit max entry limit (%s)",
                                archive_path,
                                max_entries,
                            )
                            truncated = True
                            break
        return entries, truncated

    def _list_rar(self, archive_path: str) -> Tuple[List[dict], bool]:
        """List contents of a RAR file."""
        entries = []
        max_entries, max_member_size, max_total_size = self._archive_limits()
        convertible = self._convertible_extensions()
        total_size = 0
        truncated = False
        with rarfile.RarFile(archive_path, "r") as rf:  # type: ignore[name-defined]
            for info in rf.infolist():
                if info.is_dir():
                    continue
                if is_junk_path(info.filename):
                    continue
                try:
                    self._validate_member(info.filename)
                except ValueError:
                    continue
                ext = Path(info.filename).suffix.lower()
                if ext in convertible:
                    if max_member_size > 0 and info.file_size > max_member_size:
                        logger.warning(
                            "Skipping oversized archive member %s (%s)",
                            info.filename,
                            self._format_size(info.file_size),
                        )
                        truncated = True
                        continue
                    if max_total_size > 0 and (total_size + info.file_size) > max_total_size:
                        logger.warning(
                            "Archive %s hit max total size limit (%s)",
                            archive_path,
                            self._format_size(max_total_size),
                        )
                        truncated = True
                        break
                    total_size += info.file_size
                    entries.append(
                        {
                            "archive_path": archive_path,
                            "internal_path": info.filename,
                            "name": os.path.basename(info.filename),
                            "size": info.file_size,
                            "extension": ext,
                            "convertible": True,
                        }
                    )
                    if max_entries > 0 and len(entries) >= max_entries:
                        logger.warning(
                            "Archive %s hit max entry limit (%s)",
                            archive_path,
                            max_entries,
                        )
                        truncated = True
                        break
        return entries, truncated

    def extract_file(self, archive_path: str, internal_path: str) -> Tuple[str, str]:
        """Extract a specific file from an archive into a temp directory."""
        ext = Path(archive_path).suffix.lower()
        temp_dir = self._create_temp_dir()

        try:
            destination = self._prepare_destination(temp_dir, internal_path)
            size = self._coerce_size(self._get_member_size(archive_path, internal_path))
            if size is None:
                if self._size_limits_enabled():
                    raise ValueError(
                        "Archive member size unknown; cannot enforce limits"
                    )
            else:
                self._check_member_size(size, member=internal_path)
                self._check_total_size(size)

            if ext == ".zip":
                extracted = self._extract_from_zip(
                    archive_path, internal_path, destination
                )
            elif ext == ".7z" and HAS_7Z:
                extracted = self._extract_from_7z(
                    archive_path, internal_path, destination
                )
            elif ext == ".rar" and HAS_RAR:
                extracted = self._extract_from_rar(
                    archive_path, internal_path, destination
                )
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
        if normalized.startswith("/"):
            raise ValueError("Absolute paths are not allowed inside archives")
        segments = normalized.split("/")
        if not segments or any(part in ("", ".", "..") for part in segments):
            raise ValueError("Invalid archive member path")
        if any(":" in part for part in segments):
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

    def _extract_from_zip(
        self, archive_path: str, internal_path: str, destination: str
    ) -> str:
        with zipfile.ZipFile(archive_path, "r") as zf:
            try:
                with zf.open(internal_path) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except KeyError as exc:
                raise FileNotFoundError(
                    f"{internal_path} not found in archive"
                ) from exc
        return destination

    def extract_related_files(
        self, archive_path: str, internal_path: str, temp_dir: str
    ):
        """Extract sibling files for multi-file formats like .cue or .gdi."""
        entry_ext = Path(internal_path).suffix.lower()
        if entry_ext not in {".cue", ".gdi"}:
            return

        parent = PurePosixPath(internal_path).parent
        if str(parent) == ".":
            parent = PurePosixPath("")

        initial_total_size = 0
        primary_size = self._coerce_size(
            self._get_member_size(archive_path, internal_path)
        )
        if primary_size is None:
            if self._size_limits_enabled():
                raise ValueError("Archive member size unknown; cannot enforce limits")
        else:
            initial_total_size = primary_size
            self._check_total_size(initial_total_size)

        archive_ext = Path(archive_path).suffix.lower()
        if archive_ext == ".zip":
            self._extract_related_from_zip(
                archive_path,
                parent,
                temp_dir,
                initial_total_size,
                primary_member=internal_path,
            )
        elif archive_ext == ".7z" and HAS_7Z:
            self._extract_related_from_7z(
                archive_path,
                parent,
                temp_dir,
                initial_total_size,
                primary_member=internal_path,
            )
        elif archive_ext == ".rar" and HAS_RAR:
            self._extract_related_from_rar(
                archive_path,
                parent,
                temp_dir,
                initial_total_size,
                primary_member=internal_path,
            )
        else:
            raise ValueError(f"Unsupported archive format: {archive_ext}")

    def _get_member_size(self, archive_path: str, internal_path: str) -> Optional[int]:
        archive_ext = Path(archive_path).suffix.lower()
        try:
            if archive_ext == ".zip":
                with zipfile.ZipFile(archive_path, "r") as zf:
                    info = zf.getinfo(internal_path)
                    return info.file_size
            if archive_ext == ".7z" and HAS_7Z:
                with py7zr.SevenZipFile(archive_path, "r") as zf:  # type: ignore[name-defined]
                    for entry in zf.list():
                        if entry.filename == internal_path:
                            return entry.uncompressed
                return None
            if archive_ext == ".rar" and HAS_RAR:
                with rarfile.RarFile(archive_path, "r") as rf:  # type: ignore[name-defined]
                    info = rf.getinfo(internal_path)
                    return info.file_size
        except Exception:
            return None
        return None

    def _extract_related_from_zip(
        self,
        archive_path: str,
        parent: PurePosixPath,
        temp_dir: str,
        initial_total_size: int = 0,
        *,
        primary_member: Optional[str] = None,
    ):
        total_size = max(0, int(initial_total_size))
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if is_junk_path(info.filename):
                    continue
                try:
                    self._validate_member(info.filename)
                except ValueError:
                    continue
                if not self._is_same_parent(info.filename, parent):
                    continue
                if primary_member and info.filename == primary_member:
                    continue
                self._check_member_size(info.file_size, member=info.filename)
                total_size += info.file_size
                self._check_total_size(total_size)
                destination = self._prepare_destination(temp_dir, info.filename)
                with zf.open(info.filename) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    def _extract_related_from_7z(
        self,
        archive_path: str,
        parent: PurePosixPath,
        temp_dir: str,
        initial_total_size: int = 0,
        *,
        primary_member: Optional[str] = None,
    ):
        with py7zr.SevenZipFile(archive_path, "r") as zf:  # type: ignore[name-defined]
            targets = []
            total_size = max(0, int(initial_total_size))
            _, max_member_size, max_total_size = self._archive_limits()
            for entry in zf.list():
                if entry.is_directory:
                    continue
                name = entry.filename
                if is_junk_path(name):
                    continue
                try:
                    self._validate_member(name)
                except ValueError:
                    continue
                if self._is_same_parent(name, parent):
                    if primary_member and name == primary_member:
                        continue
                    size = self._coerce_size(entry.uncompressed)
                    if size is None:
                        if max_member_size > 0 or max_total_size > 0:
                            raise ValueError(
                                "Archive member size unknown; cannot enforce limits"
                            )
                        size = 0
                    self._check_member_size(size, member=name)
                    total_size += size
                    self._check_total_size(total_size)
                    targets.append(name)
            if targets:
                zf.extract(targets=targets, path=temp_dir)

    def _extract_related_from_rar(
        self,
        archive_path: str,
        parent: PurePosixPath,
        temp_dir: str,
        initial_total_size: int = 0,
        *,
        primary_member: Optional[str] = None,
    ):
        total_size = max(0, int(initial_total_size))
        with rarfile.RarFile(archive_path, "r") as rf:  # type: ignore[name-defined]
            for info in rf.infolist():
                if info.is_dir():
                    continue
                if is_junk_path(info.filename):
                    continue
                try:
                    self._validate_member(info.filename)
                except ValueError:
                    continue
                if not self._is_same_parent(info.filename, parent):
                    continue
                if primary_member and info.filename == primary_member:
                    continue
                self._check_member_size(info.file_size, member=info.filename)
                total_size += info.file_size
                self._check_total_size(total_size)
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

    @staticmethod
    def _output_name_for_member(member: str) -> str:
        """Flattened *filename* (subdirectories collapsed) that preserves the
        member's original extension.

        Tools whose output extension is derived from the input, z3ds maps
        ``.3ds`` -> ``.z3ds``, ``.cci`` -> ``.zcci``, need the original
        extension to pick the right output name. ``_output_stem_for_member``
        drops it (it exists only for chd existing-output detection, which is
        always ``<stem>.chd``), so archive conversions route the output path
        through this helper instead. Each tool's ``get_output_path_for_mode``
        treats the result as a normal filename when ``treat_as_stem=True``.
        """
        stem = ArchiveService._output_stem_for_member(member)
        suffix = PurePosixPath(member).suffix
        return f"{stem}{suffix}"

    def _extract_from_7z(
        self, archive_path: str, internal_path: str, destination: str
    ) -> str:
        with py7zr.SevenZipFile(archive_path, "r") as zf:  # type: ignore[name-defined]
            try:
                parts = PurePosixPath(internal_path).parts
                if not parts:
                    raise FileNotFoundError(f"{internal_path} not found in archive")
                base_dir = Path(destination).parents[len(parts) - 1]
                zf.extract(targets=[internal_path], path=str(base_dir))
            except Exception as exc:
                raise FileNotFoundError(
                    f"{internal_path} not found in archive"
                ) from exc

        if not os.path.isfile(destination):
            raise FileNotFoundError(f"{internal_path} not found in archive")
        return destination

    def _extract_from_rar(
        self, archive_path: str, internal_path: str, destination: str
    ) -> str:
        with rarfile.RarFile(archive_path, "r") as rf:  # type: ignore[name-defined]
            try:
                with rf.open(internal_path) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except (rarfile.Error, KeyError) as exc:  # type: ignore[name-defined]
                raise FileNotFoundError(
                    f"{internal_path} not found in archive"
                ) from exc
        return destination

    def cleanup_temp_dir(self, temp_dir: str):
        """Clean up a temporary extraction directory."""
        shutil.rmtree(temp_dir, ignore_errors=True)


archive_service = ArchiveService()
