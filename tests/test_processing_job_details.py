import time

from app.services.media_processing import ProcessingFileState
from app.services.normalization import NormalizationJob
from app.services.optimization import OptimizationJob


def test_processing_file_state_reports_live_and_finished_elapsed_time():
    item = ProcessingFileState(
        relative_path="Canal 1/Series/Test/E01.mkv",
        action="transcode",
        status="processing",
        started_at=time.time() - 3,
    )
    live = item.to_dict()
    assert live["elapsed_seconds"] >= 2.5

    item.finished_at = item.started_at + 7
    finished = item.to_dict()
    assert 6.9 <= finished["elapsed_seconds"] <= 7.1


def test_normalization_summary_stays_light_but_details_include_files():
    file_state = ProcessingFileState(
        relative_path="Canal 1/Series/Test/E01.mkv",
        action="remux",
        status="completed",
        result="Remux completado",
    )
    job = NormalizationJob(
        id=4,
        total=1,
        processed=1,
        converted=1,
        remuxed=1,
        status="completed",
        started_at=100.0,
        finished_at=105.0,
        files=[file_state],
    )

    summary = job.to_dict()
    details = job.details_dict()
    assert "files" not in summary
    assert summary["elapsed_seconds"] == 5.0
    assert details["files"][0]["status"] == "completed"
    assert details["files"][0]["action"] == "remux"


def test_optimization_details_keep_pending_processing_and_completed_states():
    files = [
        ProcessingFileState("Canal 1/a.mp4", "optimize", status="completed"),
        ProcessingFileState("Canal 1/b.mp4", "optimize", status="processing"),
        ProcessingFileState("Canal 1/c.mp4", "optimize", status="pending"),
    ]
    job = OptimizationJob(
        id=7,
        status="running",
        total=3,
        processed=1,
        optimized=1,
        current_file="Canal 1/b.mp4",
        started_at=time.time() - 2,
        files=files,
    )

    details = job.details_dict()
    assert [item["status"] for item in details["files"]] == [
        "completed",
        "processing",
        "pending",
    ]
    assert details["elapsed_seconds"] >= 1.5


def test_processing_managers_prune_old_finished_job_details():
    normalization = __import__(
        "app.services.normalization", fromlist=["NormalizationManager", "NormalizationJob"]
    )
    optimization = __import__(
        "app.services.optimization", fromlist=["OptimizationManager", "OptimizationJob"]
    )

    norm_manager = normalization.NormalizationManager()
    norm_manager._jobs = {
        job_id: normalization.NormalizationJob(id=job_id, status="completed")
        for job_id in range(1, 6)
    }
    norm_manager._prune_finished_jobs(keep=3)
    assert set(norm_manager._jobs) == {3, 4, 5}

    opt_manager = optimization.OptimizationManager()
    opt_manager._jobs = {
        job_id: optimization.OptimizationJob(id=job_id, status="completed")
        for job_id in range(1, 6)
    }
    opt_manager._prune_finished_jobs(keep=3)
    assert set(opt_manager._jobs) == {3, 4, 5}
