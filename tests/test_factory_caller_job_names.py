"""Adopters run factory workflows through a thin caller job, which prefixes job names."""

from rotisserie.factory_console.github import Obj, job_name
from rotisserie.factory_console.tracker import Stage, read_dispatcher


def test_job_name_drops_only_the_caller_prefix() -> None:
    assert job_name({"name": "rotisserie / run / factory"}) == "run / factory"
    assert job_name({"name": "run / factory"}) == "run / factory"
    assert job_name({"name": "other / dispatch"}) == "other / dispatch"
    assert job_name({}) == ""


def test_scheduler_job_is_found_behind_the_caller_prefix() -> None:
    jobs: list[Obj] = [
        {"id": 1, "name": "rotisserie / chain_rescue", "status": "completed", "steps": []},
        {"id": 2, "name": "rotisserie / dispatch", "status": "queued", "steps": []},
    ]
    assert read_dispatcher({"status": "in_progress"}, jobs).stage is Stage.WAITING_MACHINE
