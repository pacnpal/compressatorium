import asyncio
import re
import os
from pathlib import Path
from typing import AsyncGenerator, Optional

from app.config import settings


CONVERTIBLE_EXTENSIONS = {".gdi", ".iso", ".cue", ".bin"}


class ChdmanService:
    """Wrapper for chdman binary."""

    def __init__(self):
        self.chdman_path = settings.chdman_path

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "createcd"
    ) -> AsyncGenerator[dict, None]:
        """
        Run chdman conversion and yield progress updates.

        Args:
            input_path: Path to input file (GDI, ISO, CUE)
            output_path: Path for output CHD file
            mode: "createcd" or "createdvd"

        Yields:
            dict: {"progress": int, "message": str}
        """
        # Ensure output directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        cmd = [self.chdman_path, mode, "-f", "-i", input_path, "-o", output_path]

        if mode == "createdvd":
            # Insert -hs 2048 after mode for PSP compatibility
            cmd = [self.chdman_path, mode, "-hs", "2048", "-f", "-i", input_path, "-o", output_path]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        buffer = ""
        while True:
            chunk = await process.stdout.read(100)
            if not chunk:
                break

            buffer += chunk.decode("utf-8", errors="replace")

            # Process complete lines and progress updates
            while "\r" in buffer or "\n" in buffer:
                # Handle carriage returns (progress updates)
                if "\r" in buffer:
                    parts = buffer.split("\r")
                    for part in parts[:-1]:
                        if part.strip():
                            progress = self._parse_progress(part)
                            yield {"progress": progress, "message": part.strip()}
                    buffer = parts[-1]
                # Handle newlines
                elif "\n" in buffer:
                    parts = buffer.split("\n")
                    for part in parts[:-1]:
                        if part.strip():
                            progress = self._parse_progress(part)
                            yield {"progress": progress, "message": part.strip()}
                    buffer = parts[-1]

        # Process any remaining buffer
        if buffer.strip():
            progress = self._parse_progress(buffer)
            yield {"progress": progress, "message": buffer.strip()}

        await process.wait()

        if process.returncode != 0:
            raise RuntimeError(f"chdman failed with return code {process.returncode}")

        yield {"progress": 100, "message": "Conversion complete"}

    async def info(self, chd_path: str) -> dict:
        """Get information about a CHD file."""
        process = await asyncio.create_subprocess_exec(
            self.chdman_path, "info", "-i", chd_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(stderr.decode() or f"chdman info failed with code {process.returncode}")

        return self._parse_info(stdout.decode())

    def _parse_progress(self, line: str) -> int:
        """Parse chdman output for progress percentage."""
        # chdman outputs: "Compressing, 45.2% complete..."
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
        if match:
            return min(99, int(float(match.group(1))))
        return 0

    def _parse_info(self, output: str) -> dict:
        """Parse chdman info output into structured data."""
        info = {"raw_data": output}

        # Parse key-value pairs
        for line in output.split("\n"):
            line = line.strip()
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip().lower().replace(" ", "_")
                info[key] = value.strip()

        return info

    @staticmethod
    def is_convertible(filename: str) -> bool:
        """Check if a file is convertible to CHD."""
        ext = Path(filename).suffix.lower()
        return ext in CONVERTIBLE_EXTENSIONS

    @staticmethod
    def get_chd_path(input_path: str, output_dir: Optional[str] = None) -> str:
        """Get the output CHD path for an input file."""
        input_p = Path(input_path)
        chd_name = input_p.stem + ".chd"

        if output_dir:
            return str(Path(output_dir) / chd_name)
        else:
            return str(input_p.parent / chd_name)


chdman_service = ChdmanService()
