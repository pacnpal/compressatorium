"""Tests for the igir service layer."""
import asyncio

import pytest

from app.models import IgirCommand, IgirJobCreateRequest, IgirLinkType
from app.services.igir import IgirProcessError, IgirService

# Import settings from the same module identity the service uses, so
# monkeypatch targets the correct object.
from config import settings as _settings


@pytest.fixture
def igir_svc():
    return IgirService()


@pytest.fixture
def basic_request():
    return IgirJobCreateRequest(
        commands=[IgirCommand.COPY],
        input_paths=["/data/games/roms"],
        output_path="/data/games/sorted",
    )


# ──────────────── _build_command ────────────────


class TestBuildCommand:
    def test_basic_copy(self, igir_svc, basic_request):
        cmd = igir_svc._build_command(basic_request)
        assert "copy" in cmd
        assert "--input" in cmd
        assert "/data/games/roms" in cmd
        assert "--output" in cmd
        assert "/data/games/sorted" in cmd

    def test_multiple_commands(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY, IgirCommand.ZIP, IgirCommand.TEST],
            input_paths=["/data/roms"],
            output_path="/data/out",
        )
        cmd = igir_svc._build_command(req)
        # Commands should appear in order
        copy_idx = cmd.index("copy")
        zip_idx = cmd.index("zip")
        test_idx = cmd.index("test")
        assert copy_idx < zip_idx < test_idx

    def test_dat_paths(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            dat_paths=["/dats/no-intro.dat", "/dats/redump.dat"],
        )
        cmd = igir_svc._build_command(req)
        dat_indices = [i for i, v in enumerate(cmd) if v == "--dat"]
        assert len(dat_indices) == 2

    def test_filter_flags(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            no_bios=True,
            no_demo=True,
            only_retail=True,
        )
        cmd = igir_svc._build_command(req)
        assert "--no-bios" in cmd
        assert "--no-demo" in cmd
        assert "--only-retail" in cmd

    def test_single_1g1r(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            single=True,
            prefer_language=["EN", "JA"],
            prefer_region=["USA", "EUR"],
            prefer_revision="newer",
        )
        cmd = igir_svc._build_command(req)
        assert "--single" in cmd
        assert "--prefer-language" in cmd
        assert "EN,JA" in cmd
        assert "--prefer-region" in cmd
        assert "USA,EUR" in cmd
        assert "--prefer-revision" in cmd
        assert "newer" in cmd

    def test_dir_organization(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            dir_dat_name=True,
            dir_letter=True,
            dir_letter_count=2,
        )
        cmd = igir_svc._build_command(req)
        assert "--dir-dat-name" in cmd
        assert "--dir-letter" in cmd
        assert "--dir-letter-count" in cmd
        assert "2" in cmd

    def test_verbosity(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            verbose=2,
        )
        cmd = igir_svc._build_command(req)
        assert "-vv" in cmd

    def test_clean_dry_run(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.CLEAN],
            input_paths=["/data/roms"],
            dat_paths=["/dats/test.dat"],
            clean_dry_run=True,
        )
        cmd = igir_svc._build_command(req)
        assert "--clean-dry-run" in cmd

    def test_overwrite_flags(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            overwrite=True,
            overwrite_invalid=True,
        )
        cmd = igir_svc._build_command(req)
        assert "--overwrite" in cmd
        assert "--overwrite-invalid" in cmd

    def test_symlink_options(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.LINK],
            input_paths=["/data/roms"],
            output_path="/data/out",
            link_mode=IgirLinkType.SYMLINK,
            symlink_relative=True,
        )
        cmd = igir_svc._build_command(req)
        assert "--link-mode" in cmd
        assert "symlink" in cmd
        assert "--symlink-relative" in cmd

    def test_relative_link_mode_sets_relative_symlink(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.LINK],
            input_paths=["/data/roms"],
            output_path="/data/out",
            link_mode=IgirLinkType.RELATIVE,
        )
        cmd = igir_svc._build_command(req)
        mode_idx = cmd.index("--link-mode")
        assert cmd[mode_idx + 1] == "symlink"
        assert "--symlink-relative" in cmd

    def test_csv_serialization_for_filters(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            filter_language=["EN", "JA"],
            filter_region=["USA", "EUR"],
        )
        cmd = igir_svc._build_command(req)
        assert "--filter-language" in cmd
        assert "EN,JA" in cmd
        assert "--filter-region" in cmd
        assert "USA,EUR" in cmd

    def test_checksum_options(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            input_checksum_quick=True,
            input_checksum_min="SHA1",
        )
        cmd = igir_svc._build_command(req)
        assert "--input-checksum-quick" in cmd
        assert "--input-checksum-min" in cmd
        assert "SHA1" in cmd

    def test_threading_options(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            dat_threads=4,
            reader_threads=8,
            writer_threads=2,
        )
        cmd = igir_svc._build_command(req)
        assert "--dat-threads" in cmd
        assert "4" in cmd
        assert "--reader-threads" in cmd
        assert "8" in cmd
        assert "--writer-threads" in cmd
        assert "2" in cmd


# ──────────────── build_command_preview ────────────────


class TestCommandPreview:
    def test_preview_strips_ionice(self, igir_svc, basic_request, monkeypatch):
        monkeypatch.setattr(_settings, "chdman_ioprio_class", 2)
        monkeypatch.setattr(_settings, "chdman_ioprio_level", 6)
        preview = igir_svc.build_command_preview(basic_request)
        assert "ionice" not in preview
        assert "copy" in preview


# ──────────────── validate_request ────────────────


class TestValidateRequest:
    def test_valid_copy(self, igir_svc, basic_request, monkeypatch):
        monkeypatch.setattr(
            "utils.path_utils.is_within_configured_volumes",
            lambda *a, **kw: True,
        )
        result = igir_svc.validate_request(basic_request)
        assert result.valid is True
        assert len(result.errors) == 0

    def test_no_commands(self, igir_svc, monkeypatch):
        monkeypatch.setattr(_settings, "chd_volumes", "/data/games")
        req = IgirJobCreateRequest(
            commands=[],
            input_paths=["/data/games/roms"],
        )
        result = igir_svc.validate_request(req)
        assert result.valid is False
        assert any("command" in e.lower() for e in result.errors)

    def test_multiple_write_commands(self, igir_svc, monkeypatch):
        monkeypatch.setattr(_settings, "chd_volumes", "/data/games")
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY, IgirCommand.MOVE],
            input_paths=["/data/games/roms"],
            output_path="/data/games/out",
        )
        result = igir_svc.validate_request(req)
        assert result.valid is False
        assert any("write command" in e.lower() for e in result.errors)

    def test_archive_without_write(self, igir_svc, monkeypatch):
        monkeypatch.setattr(_settings, "chd_volumes", "/data/games")
        req = IgirJobCreateRequest(
            commands=[IgirCommand.EXTRACT],
            input_paths=["/data/games/roms"],
        )
        result = igir_svc.validate_request(req)
        assert result.valid is False
        assert any("require copy or move" in e.lower() for e in result.errors)

    def test_write_without_output(self, igir_svc, monkeypatch):
        monkeypatch.setattr(_settings, "chd_volumes", "/data/games")
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/games/roms"],
        )
        result = igir_svc.validate_request(req)
        assert result.valid is False
        assert any("output" in e.lower() for e in result.errors)

    def test_clean_without_dats(self, igir_svc, monkeypatch):
        monkeypatch.setattr(_settings, "chd_volumes", "/data/games")
        req = IgirJobCreateRequest(
            commands=[IgirCommand.CLEAN],
            input_paths=["/data/games/roms"],
        )
        result = igir_svc.validate_request(req)
        assert result.valid is False
        assert any("dat" in e.lower() for e in result.errors)

    def test_report_without_dats(self, igir_svc, monkeypatch):
        monkeypatch.setattr(_settings, "chd_volumes", "/data/games")
        req = IgirJobCreateRequest(
            commands=[IgirCommand.REPORT],
            input_paths=["/data/games/roms"],
        )
        result = igir_svc.validate_request(req)
        assert result.valid is False
        assert any("dat" in e.lower() for e in result.errors)

    def test_zip_without_copy_move(self, igir_svc, monkeypatch):
        monkeypatch.setattr(_settings, "chd_volumes", "/data/games")
        req = IgirJobCreateRequest(
            commands=[IgirCommand.LINK, IgirCommand.ZIP],
            input_paths=["/data/games/roms"],
            output_path="/data/games/out",
        )
        result = igir_svc.validate_request(req)
        assert result.valid is False
        assert any("require copy or move" in e.lower() for e in result.errors)

    def test_conflicting_filters(self, igir_svc, monkeypatch):
        monkeypatch.setattr(_settings, "chd_volumes", "/data/games")
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/games/roms"],
            output_path="/data/games/out",
            no_bios=True,
            only_bios=True,
        )
        result = igir_svc.validate_request(req)
        assert result.valid is False
        assert any("conflicting" in e.lower() for e in result.errors)

    def test_invalid_fix_extension(self, igir_svc, monkeypatch):
        monkeypatch.setattr(_settings, "chd_volumes", "/data/games")
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/games/roms"],
            output_path="/data/games/out",
            fix_extension="invalid",
        )
        result = igir_svc.validate_request(req)
        assert result.valid is False

    def test_warning_symlink_without_link(self, igir_svc, monkeypatch):
        monkeypatch.setattr(
            "utils.path_utils.is_within_configured_volumes",
            lambda *a, **kw: True,
        )
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/games/roms"],
            output_path="/data/games/out",
            symlink=True,
        )
        result = igir_svc.validate_request(req)
        assert result.valid is True
        assert any("link" in w.lower() for w in result.warnings)

    def test_warning_prefer_without_single(self, igir_svc, monkeypatch):
        monkeypatch.setattr(
            "utils.path_utils.is_within_configured_volumes",
            lambda *a, **kw: True,
        )
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/games/roms"],
            output_path="/data/games/out",
            prefer_verified=True,
        )
        result = igir_svc.validate_request(req)
        assert result.valid is True
        assert any("1g1r" in w.lower() for w in result.warnings)

    def test_warning_symlink_relative_without_symlink_mode(self, igir_svc, monkeypatch):
        monkeypatch.setattr(
            "utils.path_utils.is_within_configured_volumes",
            lambda *a, **kw: True,
        )
        req = IgirJobCreateRequest(
            commands=[IgirCommand.LINK],
            input_paths=["/data/games/roms"],
            output_path="/data/games/out",
            link_mode=IgirLinkType.HARDLINK,
            symlink_relative=True,
        )
        result = igir_svc.validate_request(req)
        assert result.valid is True
        assert any("symlink-relative" in w.lower() for w in result.warnings)

    def test_relative_optional_output_path_rejected(self, igir_svc, monkeypatch):
        monkeypatch.setattr(
            "utils.path_utils.is_within_configured_volumes",
            lambda *a, **kw: True,
        )
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/games/roms"],
            output_path="/data/games/out",
            report_output="../../tmp/report.csv",
        )
        result = igir_svc.validate_request(req)
        assert result.valid is False
        assert any("absolute path" in e.lower() for e in result.errors)


# ──────────────── _detect_phase ────────────────


class TestDetectPhase:
    def test_scanning_phase(self, igir_svc):
        assert igir_svc._detect_phase("Scanning input files...") == "scanning"

    def test_writing_phase(self, igir_svc):
        assert igir_svc._detect_phase("Writing ROMs to output") == "writing"
        assert igir_svc._detect_phase("Copying files...") == "writing"
        assert igir_svc._detect_phase("Moving files...") == "writing"

    def test_testing_phase(self, igir_svc):
        assert igir_svc._detect_phase("Testing written files") == "testing"
        assert igir_svc._detect_phase("Verifying integrity") == "testing"

    def test_cleaning_phase(self, igir_svc):
        assert igir_svc._detect_phase("Cleaning unmatched files") == "cleaning"

    def test_reporting_phase(self, igir_svc):
        assert igir_svc._detect_phase("Reporting results") == "reporting"

    def test_done_phase(self, igir_svc):
        assert igir_svc._detect_phase("Done in 42s") == "done"

    def test_remove_line_not_misclassified_as_writing(self, igir_svc):
        assert igir_svc._detect_phase("Would remove /roms/bad.zip") is None

    def test_incomplete_line_not_misclassified_as_done(self, igir_svc):
        assert igir_svc._detect_phase("Found 3 incomplete sets") is None

    def test_unknown_line(self, igir_svc):
        assert igir_svc._detect_phase("some random output") is None


# ──────────────── _parse_progress_line ────────────────


class TestParseProgressLine:
    def test_file_count(self, igir_svc):
        result = igir_svc._parse_progress_line("Processing 42/100 files", "writing")
        assert result["files_processed"] == 42
        assert result["files_total"] == 100
        assert result["progress"] > 30  # writing phase starts at 30

    def test_percentage(self, igir_svc):
        result = igir_svc._parse_progress_line("50.0% processed", "scanning")
        assert result["progress"] == 5  # 50% of scanning phase (0-10) = 5

    def test_plain_message(self, igir_svc):
        result = igir_svc._parse_progress_line("Starting scan", "scanning")
        assert result["message"] == "Starting scan"
        assert result["phase"] == "scanning"
        assert result["progress"] == 0  # scanning starts at 0

    def test_incomplete_message_keeps_existing_phase(self, igir_svc):
        result = igir_svc._parse_progress_line("Found 3 incomplete sets", "matching")
        assert result["phase"] == "matching"
        assert result["progress"] == 20


# ──────────────── run output parsing ────────────────


class _FakeStdout:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _size):
        if self._chunks:
            return self._chunks.pop(0)
        await asyncio.sleep(0)
        return b""


class _FakeProcess:
    def __init__(self, chunks, returncode=0):
        self.stdout = _FakeStdout(chunks)
        self.returncode = None
        self.pid = 4242
        self._final_returncode = returncode

    async def wait(self):
        if self.returncode is None:
            self.returncode = self._final_returncode
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class TestRunOutputParsing:
    @pytest.mark.asyncio
    async def test_trailing_buffer_line_is_parsed(self, igir_svc, monkeypatch):
        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess([b"Would delete /tmp/a.rom", b""])

        monkeypatch.setattr(
            "app.services.igir.asyncio.create_subprocess_exec",
            _fake_create_subprocess_exec,
        )

        request = IgirJobCreateRequest(
            commands=[IgirCommand.CLEAN],
            input_paths=["/data/games/roms"],
            dat_paths=["/dats/test.dat"],
        )

        updates = []
        async for update in igir_svc.run(request):
            updates.append(update)

        assert updates[-1]["clean_dry_run_results"] == ["Would delete /tmp/a.rom"]


# ──────────────── build_options_summary ────────────────


class TestBuildOptionsSummary:
    def test_basic_summary(self):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY, IgirCommand.TEST],
            input_paths=["/data/roms"],
            output_path="/data/out",
        )
        summary = IgirService.build_options_summary(req)
        assert "copy" in summary.lower()
        assert "test" in summary.lower()
        assert "1 path" in summary.lower()

    def test_1g1r_summary(self):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            single=True,
            prefer_language=["EN", "JA"],
            prefer_region=["USA"],
        )
        summary = IgirService.build_options_summary(req)
        assert "1G1R" in summary
        assert "EN" in summary

    def test_filter_summary(self):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            only_retail=True,
            no_bios=True,
        )
        summary = IgirService.build_options_summary(req)
        assert "only-retail" in summary.lower()
        assert "no-bios" in summary.lower()


# ──────────────── list_dats ────────────────


class TestListDats:
    @pytest.mark.asyncio
    async def test_list_dats_empty_dir(self, igir_svc, tmp_path, monkeypatch):
        monkeypatch.setattr(_settings, "igir_dat_path", str(tmp_path))
        result = await igir_svc.list_dats()
        assert result.path == str(tmp_path)
        assert len(result.entries) == 0

    @pytest.mark.asyncio
    async def test_list_dats_with_files(self, igir_svc, tmp_path, monkeypatch):
        monkeypatch.setattr(_settings, "igir_dat_path", str(tmp_path))
        (tmp_path / "test.dat").write_text("<datafile/>")
        (tmp_path / "test.xml").write_text("<xml/>")
        (tmp_path / "readme.txt").write_text("not a dat")

        result = await igir_svc.list_dats()
        assert len(result.entries) == 2
        names = [e.name for e in result.entries]
        assert "test.dat" in names
        assert "test.xml" in names
        assert "readme.txt" not in names

    @pytest.mark.asyncio
    async def test_list_dats_with_subdirs(self, igir_svc, tmp_path, monkeypatch):
        monkeypatch.setattr(_settings, "igir_dat_path", str(tmp_path))
        (tmp_path / "no-intro").mkdir()
        (tmp_path / "redump").mkdir()

        result = await igir_svc.list_dats()
        assert len(result.subdirectories) == 2

    @pytest.mark.asyncio
    async def test_list_dats_path_traversal_blocked(self, igir_svc, tmp_path, monkeypatch):
        monkeypatch.setattr(_settings, "igir_dat_path", str(tmp_path))
        with pytest.raises(ValueError, match="outside"):
            await igir_svc.list_dats(subdir="../../etc")


# ──────────────── _is_within_dat_path ────────────────


class TestIsWithinDatPath:
    def test_valid_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_settings, "igir_dat_path", str(tmp_path))
        assert IgirService._is_within_dat_path(str(tmp_path / "test.dat")) is True

    def test_root_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_settings, "igir_dat_path", str(tmp_path))
        assert IgirService._is_within_dat_path(str(tmp_path)) is True

    def test_outside_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_settings, "igir_dat_path", str(tmp_path))
        assert IgirService._is_within_dat_path("/etc/passwd") is False


# ──────────────── New CLI flags ────────────────


class TestNewCLIFlags:
    def test_dir_dat_mirror(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            dir_dat_mirror=True,
        )
        cmd = igir_svc._build_command(req)
        assert "--dir-dat-mirror" in cmd

    def test_dat_ignore_parent_clone(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            dat_ignore_parent_clone=True,
        )
        cmd = igir_svc._build_command(req)
        assert "--dat-ignore-parent-clone" in cmd

    def test_patch_paths(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            patch=["/patches/ips", "/patches/bps"],
        )
        cmd = igir_svc._build_command(req)
        patch_indices = [i for i, v in enumerate(cmd) if v == "--patch"]
        assert len(patch_indices) == 2
        assert "/patches/ips" in cmd
        assert "/patches/bps" in cmd

    def test_input_exclude(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            input_exclude=["**/*.nfo", "**/*.txt"],
        )
        cmd = igir_svc._build_command(req)
        exclude_indices = [i for i, v in enumerate(cmd) if v == "--input-exclude"]
        assert len(exclude_indices) == 2
        assert "**/*.nfo" in cmd
        assert "**/*.txt" in cmd

    def test_remove_headers(self, igir_svc):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            remove_headers="all",
        )
        cmd = igir_svc._build_command(req)
        assert "--remove-headers" in cmd
        idx = cmd.index("--remove-headers")
        assert cmd[idx + 1] == "all"

    def test_temp_dir_from_settings(self, igir_svc, monkeypatch):
        monkeypatch.setattr(_settings, "igir_temp_dir", "/tmp/igir-work")
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
        )
        cmd = igir_svc._build_command(req)
        assert "--temp-dir" in cmd
        idx = cmd.index("--temp-dir")
        assert cmd[idx + 1] == "/tmp/igir-work"

    def test_no_temp_dir_when_unset(self, igir_svc, monkeypatch):
        monkeypatch.setattr(_settings, "igir_temp_dir", None)
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
        )
        cmd = igir_svc._build_command(req)
        assert "--temp-dir" not in cmd


# ──────────────── Validation for new fields ────────────────


class TestNewFieldValidation:
    def test_remove_headers_non_standard_warning(self, igir_svc, monkeypatch):
        monkeypatch.setattr(
            "utils.path_utils.is_within_configured_volumes",
            lambda *a, **kw: True,
        )
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            remove_headers="custom-header",
        )
        result = igir_svc.validate_request(req)
        assert result.valid is True
        assert any("remove-headers" in w.lower() for w in result.warnings)

    def test_remove_headers_known_no_warning(self, igir_svc, monkeypatch):
        monkeypatch.setattr(
            "utils.path_utils.is_within_configured_volumes",
            lambda *a, **kw: True,
        )
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            remove_headers="all",
        )
        result = igir_svc.validate_request(req)
        assert result.valid is True
        assert not any("remove-headers" in w.lower() for w in result.warnings)

    def test_patch_without_write_command_warning(self, igir_svc, monkeypatch):
        monkeypatch.setattr(
            "utils.path_utils.is_within_configured_volumes",
            lambda *a, **kw: True,
        )
        req = IgirJobCreateRequest(
            commands=[IgirCommand.REPORT],
            input_paths=["/data/roms"],
            patch=["/patches/ips"],
        )
        result = igir_svc.validate_request(req)
        assert any("patch" in w.lower() for w in result.warnings)

    def test_empty_input_exclude_error(self, igir_svc, monkeypatch):
        monkeypatch.setattr(
            "utils.path_utils.is_within_configured_volumes",
            lambda *a, **kw: True,
        )
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            input_exclude=["**/*.nfo", ""],
        )
        result = igir_svc.validate_request(req)
        assert result.valid is False
        assert any("exclude" in e.lower() for e in result.errors)

    def test_clean_exclude_glob_allowed(self, igir_svc, monkeypatch):
        monkeypatch.setattr(
            "utils.path_utils.is_within_configured_volumes",
            lambda *a, **kw: True,
        )
        req = IgirJobCreateRequest(
            commands=[IgirCommand.CLEAN],
            input_paths=["/data/roms"],
            dat_paths=["/dats/test.dat"],
            clean_exclude=["**/*.txt"],
        )
        result = igir_svc.validate_request(req)
        assert result.valid is True
        assert not any("clean exclude path" in e.lower() for e in result.errors)


# ──────────────── Output capture patterns ────────────────


class TestOutputCapturePatterns:
    def test_files_found_pattern(self):
        from app.services.igir import _FILES_FOUND_RE
        match = _FILES_FOUND_RE.search("Found 123 files in input")
        assert match is not None
        assert match.group(1) == "123"

    def test_report_line_pattern(self):
        from app.services.igir import _REPORT_LINE_RE
        assert _REPORT_LINE_RE.search("Wrote report.csv") is not None
        assert _REPORT_LINE_RE.search("fixdat output") is not None
        assert _REPORT_LINE_RE.search("dir2dat created") is not None
        assert _REPORT_LINE_RE.search("random output") is None

    def test_clean_dry_run_pattern(self):
        from app.services.igir import _CLEAN_DRY_RE
        assert _CLEAN_DRY_RE.search("Would delete /roms/bad.zip") is not None
        assert _CLEAN_DRY_RE.search("Clean dry run: 5 files") is not None
        assert _CLEAN_DRY_RE.search("Copying files") is None


# ──────────────── build_options_summary with new fields ────────────────


class TestBuildOptionsSummaryNewFields:
    def test_dir_dat_mirror_in_summary(self):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            dir_dat_mirror=True,
        )
        summary = IgirService.build_options_summary(req)
        assert "dat mirror" in summary.lower()

    def test_remove_headers_in_summary(self):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            remove_headers="all",
        )
        summary = IgirService.build_options_summary(req)
        assert "remove headers" in summary.lower()

    def test_patch_in_summary(self):
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
            patch=["/patches/a", "/patches/b"],
        )
        summary = IgirService.build_options_summary(req)
        assert "patches" in summary.lower()
        assert "2" in summary


# ──────────────── IgirProcessError ────────────────


class TestIgirProcessError:
    def test_stores_output_log(self):
        log = ["line1", "line2", "line3"]
        err = IgirProcessError("igir failed with exit code 1", output_log=log)
        assert err.output_log == log
        assert "exit code 1" in str(err)

    def test_default_empty_log(self):
        err = IgirProcessError("igir failed")
        assert err.output_log == []

    def test_none_log_becomes_empty_list(self):
        err = IgirProcessError("igir failed", output_log=None)
        assert err.output_log == []

    def test_inherits_runtime_error(self):
        err = IgirProcessError("msg")
        assert isinstance(err, RuntimeError)


# ──────────────── Output log buffer size ────────────────


class TestOutputLogBufferSize:
    def test_command_preview_includes_all_flags(self, igir_svc):
        """Command preview captures the full command string."""
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY, IgirCommand.TEST],
            input_paths=["/data/roms"],
            output_path="/data/out",
            dir_dat_name=True,
            single=True,
        )
        preview = igir_svc.build_command_preview(req)
        assert "copy" in preview
        assert "test" in preview
        assert "--input" in preview
        assert "--output" in preview
        assert "--dir-dat-name" in preview
        assert "--single" in preview

    def test_command_preview_strips_ionice(self, igir_svc, monkeypatch):
        """Command preview strips ionice wrapper for readability."""
        monkeypatch.setattr(_settings, "chdman_ioprio_class", 2)
        monkeypatch.setattr(_settings, "chdman_ioprio_level", 7)
        req = IgirJobCreateRequest(
            commands=[IgirCommand.COPY],
            input_paths=["/data/roms"],
            output_path="/data/out",
        )
        preview = igir_svc.build_command_preview(req)
        # Preview should not contain ionice
        assert "ionice" not in preview
        assert "copy" in preview
