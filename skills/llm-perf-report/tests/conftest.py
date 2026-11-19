# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------
"""Shared helpers: load the skill scripts and build synthetic endpoint reports."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPTS = Path(__file__).parents[1] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"llm_perf_report_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses look up their module in sys.modules while the module executes.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def combine_module():
    """The combine.py script as a module."""
    return _load("combine")


@pytest.fixture(scope="session")
def slides_module():
    """The render_slides.py script as a module."""
    return _load("render_slides")


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_request(
    prompt_id,
    *,
    completion_tokens=101,
    decode_ms=1000.0,
    ttft_ms=50.0,
    response="same",
    cohorts=None,
    pass_index=0,
    index=0,
    kind="timed",
    draft=None,
    cached_prompt_tokens=0,
    token_events=None,
):
    """One request entry; `draft` is (drafted, accepted) or None."""
    events = completion_tokens if token_events is None else token_events
    intervals = [round(decode_ms / (events - 1), 3)] * (events - 1) if events > 1 else []
    drafted, accepted = draft if draft else (None, None)
    rate = None if draft is None or not drafted else accepted / drafted
    timings = None
    if draft is not None:
        timings = {
            "predicted_n": completion_tokens,
            "draft_n": drafted,
            "draft_n_accepted": accepted,
        }
    return {
        "kind": kind,
        "index": index,
        "pass": pass_index if kind == "timed" else None,
        "prompt_id": prompt_id,
        "row_index": 0,
        "cohorts": dict(cohorts or {}),
        "prompt_tokens": 40,
        "cached_prompt_tokens": cached_prompt_tokens,
        "completion_tokens": completion_tokens,
        "completion_tokens_source": "usage",
        "finish_reason": "length",
        "ttft_ms": ttft_ms,
        "request_duration_ms": ttft_ms + decode_ms,
        "decode_duration_ms": decode_ms,
        "decode_tokens_per_second": round((completion_tokens - 1) * 1000.0 / decode_ms, 2),
        "token_event_count": events,
        "tokens_per_event": round(completion_tokens / events, 3),
        "token_event_intervals_ms": intervals,
        "server_prefill_tokens": None,
        "server_prefill_ms": None,
        "server_prefill_tokens_per_second": None,
        "server_decode_tokens": None,
        "server_decode_ms": None,
        "server_decode_tokens_per_second": None,
        "draft_tokens": drafted,
        "draft_tokens_accepted": accepted,
        "draft_acceptance_rate": rate,
        "server_timings": timings,
        "response_text": response,
        "response_sha256": _sha(response),
    }


def make_report(
    requests,
    *,
    labels=None,
    run_index=0,
    model_id="example-model",
    max_new_tokens=128,
    extra_body=None,
    prompts_sha256="a" * 64,
    cohort_columns=("category",),
    timestamp="2026-01-01T10:00:00+00:00",
    gpu_driver="1.0.0",
    fingerprint="server-1",
):
    """A schema-valid `winml perf --endpoint` report around the given request entries."""
    totals = dict.fromkeys(
        (
            "prompt_tokens",
            "cached_prompt_tokens",
            "completion_tokens",
            "decode_tokens",
            "decode_duration_ms",
            "decode_tokens_per_second",
            "draft_tokens",
            "draft_tokens_accepted",
            "draft_acceptance_rate",
        )
    )
    timed = [request for request in requests if request["kind"] == "timed"]
    return {
        "schema_version": 4,
        "benchmark_info": {
            "runtime": "openai-endpoint",
            "api": "openai.chat.completions",
            "timing_source": "http-stream",
            "experimental": True,
            "endpoint": "http://127.0.0.1:8080/v1",
            "model_id": model_id,
            "iterations": 1,
            "warmup": 0,
            "max_new_tokens": max_new_tokens,
            "prompt": None,
            "prompt_source": "--dataset",
            "request_parameters": {"temperature": 0, "extra_body": dict(extra_body or {})},
            "packages": {"winml-cli": "0.0.0", "httpx": "0.0.0"},
            "timestamp": timestamp,
        },
        "provenance": {
            "run_index": run_index,
            "run_labels": dict(labels or {}),
            "file_hashes": {
                "model": {"name": "model.gguf", "size_bytes": 1024, "sha256": "b" * 64}
            },
            "host": {
                "platform": "Windows-11",
                "machine": "AMD64",
                "os": {"caption": "Example OS", "version": "1.0"},
                "cpus": [{"name": "Example CPU"}],
                "gpus": [{"name": "Example GPU", "driverVersion": gpu_driver}],
                "ram_mib": 16384,
                "errors": [],
            },
            "server": {
                "endpoint": "http://127.0.0.1:8080/v1",
                "system_fingerprint": fingerprint,
                "models": [{"id": model_id}],
            },
        },
        "dataset": {
            "source": "local",
            "path": "prompts",
            "name": None,
            "revision": None,
            "split": "test",
            "samples": None,
            "shuffle": False,
            "seed": 42,
            "column_mapping": {
                "input_column": "prompt",
                "id_column": "id",
                "cohort_column": list(cohort_columns),
            },
            "row_count": len(timed),
            "selected_row_count": len(timed),
            "cohort_columns": list(cohort_columns),
            "prompts_sha256": prompts_sha256,
        },
        "requests": requests,
        "aggregate": {
            "warmup_excluded": True,
            "warmup_request_count": len(requests) - len(timed),
            "timed_request_count": len(timed),
            "ttft_ms": None,
            "request_duration_ms": None,
            "decode_tokens_per_second": None,
            "totals": totals,
            "finish_reason_counts": {"length": len(timed)},
            "cohorts": {},
        },
    }


def two_prompt_requests(scale=1.0, responses=("alpha", "beta"), draft=None, ttft_ms=50.0):
    """Prompt a (coding) decodes 100 tokens/s and prompt b (writing) 20 tokens/s, times `scale`."""
    return [
        make_request(
            "a",
            completion_tokens=101,
            decode_ms=1000.0 / scale,
            ttft_ms=ttft_ms,
            response=responses[0],
            cohorts={"category": "coding"},
            index=0,
            draft=draft,
        ),
        make_request(
            "b",
            completion_tokens=11,
            decode_ms=500.0 / scale,
            ttft_ms=ttft_ms,
            response=responses[1],
            cohorts={"category": "writing"},
            index=1,
            draft=draft,
        ),
    ]


def write_reports(folder, reports):
    """Write {file name: report} into `folder` and return the paths."""
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, report in reports.items():
        path = folder / name
        path.write_text(json.dumps(report), encoding="utf-8")
        paths.append(path)
    return paths


@pytest.fixture(scope="session")
def build():
    """Report builders for tests."""
    return SimpleNamespace(
        request=make_request,
        report=make_report,
        two_prompts=two_prompt_requests,
        write=write_reports,
    )


@pytest.fixture
def plain_and_draft_reports():
    """Two interleaved runs each of a plain and a draft configuration.

    The draft configuration decodes twice as fast. Plain runs agree on prompt a
    and differ on prompt b; draft runs repeat plain run 0's responses.
    """
    plain = {"server": "llama.cpp", "config": "plain"}
    draft = {"server": "llama.cpp", "config": "draft"}
    return {
        "plain-0.json": make_report(two_prompt_requests(), labels=plain, run_index=0),
        "draft-0.json": make_report(
            two_prompt_requests(2.0, draft=(100, 75), ttft_ms=40.0), labels=draft, run_index=0
        ),
        "plain-1.json": make_report(
            two_prompt_requests(responses=("alpha", "beta-changed")), labels=plain, run_index=1
        ),
        "draft-1.json": make_report(
            two_prompt_requests(2.0, draft=(100, 75), ttft_ms=40.0), labels=draft, run_index=1
        ),
    }
