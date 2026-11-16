"""The RomM swap spike (#382) wipes its --work folder only when an earlier run
marked it. Runs the script's own self-check, which needs no Docker."""
import importlib.util
from pathlib import Path

import pytest

SPIKE = Path(__file__).resolve().parents[1] / "scripts" / "spike" / "romm_swap" / "spike.py"


@pytest.fixture(scope="module")
def spike():
    spec = importlib.util.spec_from_file_location("romm_swap_spike", SPIKE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_self_check_passes(spike):
    assert spike.self_check() == []


def test_unrelated_folder_with_env_is_refused_and_kept(spike, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (project / "main.py").write_text("print('mine')\n", encoding="utf-8")
    with pytest.raises(spike.WorkDirRefused):
        spike.work_dir_state(project)
    assert (project / "main.py").exists()


def test_marked_folder_is_accepted(spike, tmp_path):
    work = tmp_path / "work"
    spike.mark_work_dir(work)
    (work / ".env").write_text("SPIKE_PORT=1\n", encoding="utf-8")
    assert spike.work_dir_state(work) == "marked"
