import os


def compute_progress_stall_timeout(
    *,
    input_path: str,
    base_timeout: int,
    timeout_per_gib: int,
    timeout_cap: int,
) -> int:
    """Compute an adaptive stall timeout from baseline + input size."""
    baseline = max(0, int(base_timeout or 0))
    if baseline <= 0:
        return 0

    per_gib = max(0, int(timeout_per_gib or 0))
    cap = max(0, int(timeout_cap or 0))
    if per_gib <= 0:
        return min(baseline, cap) if cap > 0 else baseline

    try:
        input_size = max(0, int(os.path.getsize(input_path)))
    except OSError:
        input_size = 0

    gib = input_size / float(1024 ** 3)
    adaptive = baseline + int(gib * per_gib)
    timeout = max(baseline, adaptive)
    if cap > 0:
        timeout = min(timeout, cap)
    return timeout
