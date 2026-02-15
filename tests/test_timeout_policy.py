from app.services.timeout_policy import compute_progress_stall_timeout


def test_compute_progress_stall_timeout_uses_baseline_when_path_missing():
    timeout = compute_progress_stall_timeout(
        input_path="/path/does/not/exist.iso",
        base_timeout=600,
        timeout_per_gib=120,
        timeout_cap=7200,
    )
    assert timeout == 600


def test_compute_progress_stall_timeout_scales_with_input_size(monkeypatch):
    two_gib = 2 * (1024 ** 3)
    monkeypatch.setattr("app.services.timeout_policy.os.path.getsize", lambda _: two_gib)

    timeout = compute_progress_stall_timeout(
        input_path="/tmp/fake.iso",
        base_timeout=600,
        timeout_per_gib=120,
        timeout_cap=7200,
    )
    assert timeout == 840


def test_compute_progress_stall_timeout_respects_cap(monkeypatch):
    fifty_gib = 50 * (1024 ** 3)
    monkeypatch.setattr("app.services.timeout_policy.os.path.getsize", lambda _: fifty_gib)

    timeout = compute_progress_stall_timeout(
        input_path="/tmp/fake.iso",
        base_timeout=600,
        timeout_per_gib=300,
        timeout_cap=1800,
    )
    assert timeout == 1800
