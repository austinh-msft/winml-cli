# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4.23"]
# ///
# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------
"""Combine `winml perf --endpoint` reports into one comparison file.

Each input is one report (`schema_version` 4). Reports are grouped into series
by their `provenance.run_labels`; the timed requests of every run in a series
are pooled. Each series is summarized overall and per cohort, compared with a
baseline series, and checked for identical outputs within the series and
against the baseline. The result is one JSON file (`combined_version` 1) that
`render_slides.py` turns into a deck.

Only the standard library is required. When `jsonschema` is importable and the
report schema is found, every report is also validated against it.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import itertools
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


COMBINED_VERSION = 1
REPORT_SCHEMA_VERSION = 4
REPORT_RUNTIME = "openai-endpoint"
SCHEMA_RELATIVE_PATH = Path("src/winml/modelkit/commands/assets/perf-endpoint-report.schema.json")

# Label keys that number repeats rather than name a configuration.
RUN_INDEX_LIKE = re.compile(
    r"^(run([-_ ]?(index|idx|id|number|num|no))?|index|idx|repeat|rep|replicate"
    r"|iteration|iter|trial|attempt|seed)$",
    re.IGNORECASE,
)

RUN_RANGE_FASTER = "faster_in_every_run"
RUN_RANGE_SLOWER = "slower_in_every_run"
RUN_RANGE_OVERLAP = "overlaps_baseline_runs"
RUN_RANGE_NOT_ENOUGH = "not_enough_runs"


class ReportError(ValueError):
    """An input report cannot be used."""


@dataclass
class Run:
    """One input report."""

    file: str
    file_sha256: str
    report: dict[str, Any]
    labels: dict[str, str]
    run_index: int | None
    timed: list[dict[str, Any]] = field(default_factory=list)

    @property
    def info(self) -> dict[str, Any]:
        """The report's `benchmark_info`."""
        return self.report.get("benchmark_info") or {}

    @property
    def provenance(self) -> dict[str, Any]:
        """The report's `provenance`."""
        return self.report.get("provenance") or {}

    @property
    def dataset(self) -> dict[str, Any] | None:
        """The report's `dataset`, or None for a single-prompt run."""
        return self.report.get("dataset")


# =============================================================================
# Inputs
# =============================================================================


def expand_inputs(items: list[str]) -> list[Path]:
    """Resolve files, folders (their `*.json`) and glob patterns, in a stable order."""
    paths: list[Path] = []
    for item in items:
        path = Path(item)
        if path.is_dir():
            found = sorted(path.glob("*.json"))
        elif glob.has_magic(item):
            # Path.glob does not accept absolute patterns such as D:\results\*.json.
            matches = glob.glob(item, recursive=True)  # noqa: PTH207
            found = sorted(Path(match) for match in matches)
        else:
            if not path.is_file():
                raise ReportError(f"Input not found: {item}")
            found = [path]
        if not found:
            raise ReportError(f"No JSON reports match: {item}")
        paths.extend(found)
    unique: dict[Path, Path] = {}
    for path in paths:
        unique.setdefault(path.resolve(), path)
    return list(unique.values())


def find_schema(explicit: str | None = None) -> Path | None:
    """Locate the report JSON Schema: an explicit path, or the repository copy."""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ReportError(f"Schema not found: {explicit}")
        return path
    for parent in Path(__file__).resolve().parents:
        candidate = parent / SCHEMA_RELATIVE_PATH
        if candidate.is_file():
            return candidate
    return None


def make_validator(schema_path: Path | None) -> Any | None:
    """Return a JSON Schema validator, or None when jsonschema or the schema is missing."""
    if schema_path is None:
        return None
    try:
        import jsonschema
    except ImportError:
        return None
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def check_report(report: Any, name: str, validator: Any | None = None) -> None:
    """Raise ReportError when a report is not a usable endpoint report."""
    if not isinstance(report, dict):
        raise ReportError(f"{name}: not a JSON object")
    version = report.get("schema_version")
    if version != REPORT_SCHEMA_VERSION:
        raise ReportError(
            f"{name}: schema_version {version!r} is not supported; "
            f"expected {REPORT_SCHEMA_VERSION} from `winml perf --endpoint`"
        )
    info = report.get("benchmark_info")
    if not isinstance(info, dict) or info.get("runtime") != REPORT_RUNTIME:
        raise ReportError(f"{name}: benchmark_info.runtime is not {REPORT_RUNTIME!r}")
    for key, kind in (("provenance", dict), ("requests", list), ("aggregate", dict)):
        if not isinstance(report.get(key), kind):
            raise ReportError(f"{name}: missing or invalid {key!r}")
    if validator is not None:
        errors = sorted(validator.iter_errors(report), key=lambda error: list(error.path))
        if errors:
            first = errors[0]
            location = "/".join(str(part) for part in first.path) or "(root)"
            raise ReportError(
                f"{name}: does not match the report schema at {location}: {first.message}"
            )


def load_run(path: Path, validator: Any | None = None) -> Run:
    """Read and check one report."""
    data = path.read_bytes()
    try:
        report = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReportError(f"{path.name}: not valid JSON ({error})") from error
    check_report(report, path.name, validator)
    provenance = report["provenance"]
    labels = {str(key): str(value) for key, value in (provenance.get("run_labels") or {}).items()}
    timed = [request for request in report["requests"] if request.get("kind") == "timed"]
    return Run(
        file=path.name,
        file_sha256=hashlib.sha256(data).hexdigest(),
        report=report,
        labels=labels,
        run_index=provenance.get("run_index"),
        timed=timed,
    )


# =============================================================================
# Statistics
# =============================================================================


def _percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile, as `winml perf` reports it."""
    index = min(int(len(sorted_values) * p / 100), len(sorted_values) - 1)
    return sorted_values[index]


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def stats(values: list[float]) -> dict[str, Any] | None:
    """Summary of a list of values, or None when it is empty."""
    if not values:
        return None
    ordered = sorted(values)
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return {
        "count": len(values),
        "mean": _round(mean),
        "std": _round(variance**0.5),
        "min": _round(ordered[0]),
        "max": _round(ordered[-1]),
        "p50": _round(_percentile(ordered, 50)),
        "p90": _round(_percentile(ordered, 90)),
    }


def median(values: list[float]) -> float | None:
    """Middle value (mean of the two middle values for an even count)."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _values(requests: list[dict[str, Any]], key: str) -> list[float]:
    return [number for request in requests if (number := _number(request.get(key))) is not None]


def _decode_pair(request: dict[str, Any]) -> tuple[float, float] | None:
    """Decode tokens (all but the first) and decode time, as `winml perf` pools them."""
    completion = _number(request.get("completion_tokens"))
    duration = _number(request.get("decode_duration_ms"))
    if completion is None or completion < 2 or duration is None or duration <= 0:
        return None
    return completion - 1, duration


def pooled_decode_rate(requests: list[dict[str, Any]]) -> float | None:
    """Total decode tokens divided by total decode time, in tokens per second."""
    pairs = [pair for request in requests if (pair := _decode_pair(request)) is not None]
    if not pairs:
        return None
    tokens = sum(tokens for tokens, _ in pairs)
    duration_ms = sum(duration for _, duration in pairs)
    return tokens * 1000.0 / duration_ms


def summarize(requests: list[dict[str, Any]], run_count: int) -> dict[str, Any]:
    """Metrics of a group of timed requests."""
    decode = stats(_values(requests, "decode_tokens_per_second"))
    if decode is not None:
        decode["pooled"] = _round(pooled_decode_rate(requests))
    tokens_per_event = stats(_values(requests, "tokens_per_event"))
    events = [
        (completion, count)
        for request in requests
        if (completion := _number(request.get("completion_tokens"))) is not None
        and (count := _number(request.get("token_event_count")))
    ]
    if tokens_per_event is not None and events:
        tokens_per_event["pooled"] = _round(
            sum(completion for completion, _ in events) / sum(count for _, count in events)
        )
    draft_pairs = [
        (drafted, accepted)
        for request in requests
        if (drafted := _number(request.get("draft_tokens"))) is not None
        and (accepted := _number(request.get("draft_tokens_accepted"))) is not None
    ]
    draft = None
    if draft_pairs:
        drafted = sum(value for value, _ in draft_pairs)
        accepted = sum(value for _, value in draft_pairs)
        draft = {
            "request_count": len(draft_pairs),
            "draft_tokens": drafted,
            "draft_tokens_accepted": accepted,
            "acceptance_rate": _round(accepted / drafted, 4) if drafted else None,
        }
    reasons: dict[str, int] = {}
    for request in requests:
        reason = request.get("finish_reason") or "none"
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "request_count": len(requests),
        "run_count": run_count,
        "decode_tokens_per_second": decode,
        "ttft_ms": stats(_values(requests, "ttft_ms")),
        "request_duration_ms": stats(_values(requests, "request_duration_ms")),
        "server_decode_tokens_per_second": stats(
            _values(requests, "server_decode_tokens_per_second")
        ),
        "tokens_per_event": tokens_per_event,
        "prompt_tokens": stats(_values(requests, "prompt_tokens")),
        "completion_tokens": stats(_values(requests, "completion_tokens")),
        "cached_prompt_tokens_total": sum(_values(requests, "cached_prompt_tokens")),
        "draft": draft,
        "finish_reason_counts": dict(sorted(reasons.items())),
    }


def natural_key(value: str) -> list[tuple[int, int, str]]:
    """Sort key that orders `2k` before `16k`."""
    return [
        (0, int(part), "") if part.isdigit() else (1, 0, part.lower())
        for part in re.split(r"(\d+)", value)
        if part
    ]


def cohort_columns(runs: list[Run]) -> list[str]:
    """Cohort columns in first-seen order across all reports."""
    columns: list[str] = []
    for run in runs:
        dataset = run.dataset or {}
        for column in dataset.get("cohort_columns") or []:
            if column not in columns:
                columns.append(column)
        for request in run.timed:
            for column in request.get("cohorts") or {}:
                if column not in columns:
                    columns.append(column)
    return columns


def summarize_cohorts(runs: list[Run], columns: list[str]) -> dict[str, dict[str, dict[str, Any]]]:
    """Per cohort column and value: metrics pooled over the runs."""
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for column in columns:
        groups: dict[str, list[dict[str, Any]]] = {}
        run_sets: dict[str, set[int]] = {}
        for position, run in enumerate(runs):
            for request in run.timed:
                value = (request.get("cohorts") or {}).get(column)
                if value is None:
                    continue
                groups.setdefault(str(value), []).append(request)
                run_sets.setdefault(str(value), set()).add(position)
        if groups:
            result[column] = {
                value: summarize(groups[value], len(run_sets[value]))
                for value in sorted(groups, key=natural_key)
            }
    return result


# =============================================================================
# Series
# =============================================================================


def default_series_keys(runs: list[Run]) -> list[str]:
    """All run-label keys, in first-seen order, except keys that number repeats."""
    keys: list[str] = []
    for run in runs:
        for key in run.labels:
            if key not in keys and not RUN_INDEX_LIKE.match(key):
                keys.append(key)
    return keys


def _series_identity(run: Run, keys: list[str]) -> tuple[tuple[str, str | None], ...]:
    if keys:
        return tuple((key, run.labels.get(key)) for key in keys)
    return (("model_id", str(run.info.get("model_id"))),)


def group_series(runs: list[Run], keys: list[str]) -> list[tuple[dict[str, str], list[Run]]]:
    """Group runs by their series labels, in order of first appearance."""
    groups: dict[tuple[tuple[str, str | None], ...], list[Run]] = {}
    for run in runs:
        groups.setdefault(_series_identity(run, keys), []).append(run)
    result = []
    for identity, members in groups.items():
        labels = {key: value for key, value in identity if value is not None}
        members.sort(key=lambda run: (run.run_index is None, run.run_index or 0, run.file))
        result.append((labels, members))
    return result


def series_names(all_labels: list[dict[str, str]]) -> list[str]:
    """Short names built from the label keys whose values differ between series."""
    keys: list[str] = []
    for labels in all_labels:
        keys.extend(key for key in labels if key not in keys)
    varying = [key for key in keys if len({labels.get(key) for labels in all_labels}) > 1]
    shown = varying or keys
    names = []
    for labels in all_labels:
        parts = [f"{key}={labels[key]}" for key in shown if key in labels]
        names.append(", ".join(parts) or "all runs")
    return names


def parse_baseline(spec: str) -> dict[str, str] | str:
    """`KEY=VALUE[,KEY=VALUE]` becomes a label filter; anything else is a series name."""
    pairs = [part.strip() for part in spec.split(",") if part.strip()]
    if pairs and all("=" in part for part in pairs):
        return dict(part.split("=", 1) for part in pairs)
    return spec


def pick_baseline(series: list[dict[str, Any]], spec: str | None) -> int:
    """Index of the baseline series: the one matching `spec`, or the first one."""
    if spec is None:
        return 0
    by_name = [i for i, item in enumerate(series) if item["name"] == spec]
    if len(by_name) == 1:
        return by_name[0]
    wanted = parse_baseline(spec)
    if isinstance(wanted, str):
        matches = by_name
    else:
        matches = [
            i
            for i, item in enumerate(series)
            if all(item["labels"].get(key) == value for key, value in wanted.items())
        ]
    names = ", ".join(repr(item["name"]) for item in series)
    if not matches:
        raise ReportError(f"Baseline {spec!r} matches no series. Series: {names}")
    if len(matches) > 1:
        found = ", ".join(repr(series[i]["name"]) for i in matches)
        raise ReportError(f"Baseline {spec!r} matches several series: {found}")
    return matches[0]


# =============================================================================
# Identical outputs
# =============================================================================


def response_hashes(run: Run) -> dict[tuple[str, int], str]:
    """Response SHA-256 per (prompt_id, pass) of the timed requests."""
    hashes: dict[tuple[str, int], str] = {}
    for request in run.timed:
        digest = request.get("response_sha256")
        if not digest:
            continue
        prompt = request.get("prompt_id")
        prompt = "(single prompt)" if prompt is None else str(prompt)
        index = request.get("pass")
        if index is None:
            index = request.get("index", 0)
        hashes[(prompt, int(index))] = digest
    return hashes


def compare_outputs(
    left: list[dict[tuple[str, int], str]],
    right: list[dict[tuple[str, int], str]] | None = None,
) -> dict[str, Any] | None:
    """Identical-output rate between runs, joined on (prompt_id, pass).

    Without `right`, every pair of runs in `left` is compared (the determinism
    of one series). With `right`, every run in `left` is compared with every run
    in `right`. `rate` is the share of compared request pairs whose response
    hashes are equal; `prompts_identical` counts the (prompt_id, pass) keys
    whose response was the same in every compared run. Returns None when
    nothing can be compared.
    """
    run_pairs = (
        list(itertools.combinations(left, 2))
        if right is None
        else list(itertools.product(left, right))
    )
    pairs = identical = 0
    keys: set[tuple[str, int]] = set()
    for first, second in run_pairs:
        shared = first.keys() & second.keys()
        keys |= shared
        pairs += len(shared)
        identical += sum(first[key] == second[key] for key in shared)
    if not pairs:
        return None
    involved = left if right is None else left + right
    prompts_identical = sum(
        len({hashes[key] for hashes in involved if key in hashes}) == 1 for key in keys
    )
    return {
        "run_pairs": len(run_pairs),
        "pairs": pairs,
        "identical_pairs": identical,
        "rate": _round(identical / pairs, 4),
        "prompts": len(keys),
        "prompts_identical": prompts_identical,
    }


# =============================================================================
# Comparison with the baseline
# =============================================================================


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return _round(numerator / denominator, 4)


def metric_field(metrics: dict[str, Any] | None, metric: str, key: str) -> float | None:
    """`metrics[metric][key]`, or None when either is missing."""
    block = (metrics or {}).get(metric) or {}
    return block.get(key)


def compare_metrics(metrics: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Decode speedup (above 1 is faster) and TTFT ratio (below 1 is faster)."""
    return {
        "decode_speedup_pooled": _ratio(
            metric_field(metrics, "decode_tokens_per_second", "pooled"),
            metric_field(baseline, "decode_tokens_per_second", "pooled"),
        ),
        "decode_speedup_mean": _ratio(
            metric_field(metrics, "decode_tokens_per_second", "mean"),
            metric_field(baseline, "decode_tokens_per_second", "mean"),
        ),
        "ttft_p50_ratio": _ratio(
            metric_field(metrics, "ttft_ms", "p50"), metric_field(baseline, "ttft_ms", "p50")
        ),
    }


def run_range_check(series_rates: list[float], baseline_rates: list[float]) -> str:
    """Whether every run of a series beat, or trailed, every run of the baseline."""
    if len(series_rates) < 2 or len(baseline_rates) < 2:
        return RUN_RANGE_NOT_ENOUGH
    if min(series_rates) > max(baseline_rates):
        return RUN_RANGE_FASTER
    if max(series_rates) < min(baseline_rates):
        return RUN_RANGE_SLOWER
    return RUN_RANGE_OVERLAP


# =============================================================================
# Warnings and provenance
# =============================================================================


def _warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _host_identity(run: Run) -> dict[str, Any]:
    host = run.provenance.get("host") or {}
    os_info = host.get("os") or {}
    os_parts = (os_info.get("caption"), os_info.get("version") or host.get("platform"))
    return {
        "os": " ".join(str(part) for part in os_parts if part) or None,
        "machine": host.get("machine"),
        "cpus": [cpu.get("name") for cpu in host.get("cpus") or [] if isinstance(cpu, dict)],
        "gpus": [
            {"name": gpu.get("name"), "driver_version": gpu.get("driverVersion")}
            for gpu in host.get("gpus") or []
            if isinstance(gpu, dict)
        ],
        "ram_mib": host.get("ram_mib"),
    }


def _settings(run: Run) -> dict[str, Any]:
    parameters = run.info.get("request_parameters") or {}
    return {
        "max_new_tokens": run.info.get("max_new_tokens"),
        "iterations": run.info.get("iterations"),
        "warmup": run.info.get("warmup"),
        "temperature": parameters.get("temperature"),
        "extra_body": parameters.get("extra_body"),
    }


def _distinct_per_series(
    series_runs: list[tuple[str, list[Run]]], getter: Any
) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for name, runs in series_runs:
        values: list[Any] = []
        for run in runs:
            value = getter(run)
            if value not in values:
                values.append(value)
        result[name] = values
    return result


def collect_warnings(
    series_runs: list[tuple[str, list[Run]]], columns: list[str]
) -> list[dict[str, str]]:
    """Differences that make series hard to compare, and other caveats."""
    warnings: list[dict[str, str]] = []
    all_runs = [run for _, runs in series_runs for run in runs]

    def check(code: str, what: str, getter: Any) -> None:
        per_series = _distinct_per_series(series_runs, getter)
        distinct = {
            json.dumps(value, sort_keys=True) for values in per_series.values() for value in values
        }
        if len(distinct) > 1:
            shown = "; ".join(
                f"{name}: {', '.join(json.dumps(value, sort_keys=True) for value in values)}"
                for name, values in per_series.items()
            )
            warnings.append(_warning(code, f"Runs used different {what}. {shown}"))

    check(
        "different_prompts",
        "prompts (dataset.prompts_sha256, first 12 characters)",
        lambda run: ((run.dataset or {}).get("prompts_sha256") or "")[:12] or None,
    )
    check(
        "different_max_new_tokens", "max_new_tokens", lambda run: _settings(run)["max_new_tokens"]
    )
    check("different_extra_body", "request extra_body", lambda run: _settings(run)["extra_body"])
    check("different_temperature", "temperature", lambda run: _settings(run)["temperature"])
    check("different_hosts", "hosts (OS, CPU, GPU, driver or memory)", _host_identity)

    for name, runs in series_runs:
        models = sorted({str(run.info.get("model_id")) for run in runs})
        if len(models) > 1:
            warnings.append(
                _warning(
                    "different_model_within_series",
                    f"Series {name!r} mixes model ids: {', '.join(models)}.",
                )
            )
        fingerprints = sorted(
            {str((run.provenance.get("server") or {}).get("system_fingerprint")) for run in runs}
        )
        if len(fingerprints) > 1:
            warnings.append(
                _warning(
                    "different_server_within_series",
                    f"Series {name!r} mixes server fingerprints: {', '.join(fingerprints)}.",
                )
            )
        indexes = [run.run_index for run in runs if run.run_index is not None]
        duplicates = sorted({index for index in indexes if indexes.count(index) > 1})
        if duplicates:
            warnings.append(
                _warning(
                    "duplicate_run_index",
                    f"Series {name!r} has several reports with run index "
                    f"{', '.join(map(str, duplicates))}.",
                )
            )
        if len(runs) == 1:
            warnings.append(
                _warning(
                    "single_run",
                    f"Series {name!r} has one run, so its run-to-run noise is unknown.",
                )
            )
        warnings.extend(
            _warning("missing_cohort_column", f"Series {name!r} has no cohort column {column!r}.")
            for column in columns
            if not any(
                column in (request.get("cohorts") or {}) for run in runs for request in run.timed
            )
        )
    warnings.extend(
        _warning("no_timed_requests", f"{run.file} has no timed requests.")
        for run in all_runs
        if not run.timed
    )
    cached = sum(
        _values([request for run in all_runs for request in run.timed], "cached_prompt_tokens")
    )
    if cached:
        warnings.append(
            _warning(
                "prompt_cache",
                f"The server reused {int(cached)} cached prompt tokens in timed requests, so TTFT "
                "does not measure a full prefill. On llama-server, pass "
                "--extra-body '{\"cache_prompt\": false}' to measure it.",
            )
        )
    return warnings


def _merge_by_value(items: list[tuple[dict[str, Any], str]]) -> list[dict[str, Any]]:
    """Unique items, each with the series that reported it."""
    merged: dict[str, dict[str, Any]] = {}
    for item, name in items:
        entry = merged.setdefault(json.dumps(item, sort_keys=True), {**item, "series": []})
        if name not in entry["series"]:
            entry["series"].append(name)
    return list(merged.values())


_DATASET_KEYS = (
    "source",
    "path",
    "name",
    "revision",
    "split",
    "samples",
    "selected_row_count",
    "cohort_columns",
    "prompts_sha256",
)


def _parse_timestamp(value: Any) -> datetime | None:
    """Report timestamp in UTC, or None when it is missing or unreadable."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def collect_provenance(series_runs: list[tuple[str, list[Run]]]) -> dict[str, Any]:
    """What was measured, on what, and when. Local folders are never included."""
    pairs = [(name, run) for name, runs in series_runs for run in runs]
    timestamps = sorted(
        stamp for _, run in pairs if (stamp := _parse_timestamp(run.info.get("timestamp")))
    )
    return {
        "date_range": {
            "first": timestamps[0].isoformat(timespec="seconds"),
            "last": timestamps[-1].isoformat(timespec="seconds"),
        }
        if timestamps
        else None,
        "winml_cli_versions": sorted(
            {str((run.info.get("packages") or {}).get("winml-cli")) for _, run in pairs}
        ),
        "hosts": _merge_by_value([(_host_identity(run), name) for name, run in pairs]),
        "servers": _merge_by_value(
            [
                (
                    {
                        "system_fingerprint": (run.provenance.get("server") or {}).get(
                            "system_fingerprint"
                        ),
                        "model_id": run.info.get("model_id"),
                    },
                    name,
                )
                for name, run in pairs
            ]
        ),
        "files": _merge_by_value(
            [
                (
                    {
                        "label": label,
                        **{key: entry.get(key) for key in ("name", "size_bytes", "sha256")},
                    },
                    name,
                )
                for name, run in pairs
                for label, entry in (run.provenance.get("file_hashes") or {}).items()
                if isinstance(entry, dict)
            ]
        ),
        "datasets": _merge_by_value(
            [
                (
                    {key: run.dataset.get(key) for key in _DATASET_KEYS}
                    if run.dataset
                    else {"source": "single prompt"},
                    name,
                )
                for name, run in pairs
            ]
        ),
        "settings": _merge_by_value([(_settings(run), name) for name, run in pairs]),
    }


# =============================================================================
# Combine
# =============================================================================


def _pooled_rates(rows: list[dict[str, Any]]) -> list[float]:
    return [
        row["decode_tokens_per_second_pooled"]
        for row in rows
        if row["decode_tokens_per_second_pooled"] is not None
    ]


def _run_row(run: Run) -> dict[str, Any]:
    metrics = summarize(run.timed, 1)
    return {
        "file": run.file,
        "run_index": run.run_index,
        "request_count": metrics["request_count"],
        "decode_tokens_per_second_pooled": metric_field(
            metrics, "decode_tokens_per_second", "pooled"
        ),
        "decode_tokens_per_second_mean": metric_field(metrics, "decode_tokens_per_second", "mean"),
        "ttft_ms_p50": metric_field(metrics, "ttft_ms", "p50"),
        "acceptance_rate": (metrics["draft"] or {}).get("acceptance_rate"),
    }


def _spread(rates: list[float]) -> dict[str, Any] | None:
    if not rates:
        return None
    middle = median(rates)
    return {
        "decode_tokens_per_second_pooled": {
            "min": _round(min(rates)),
            "max": _round(max(rates)),
            "median": _round(middle),
            "relative_range": _round((max(rates) - min(rates)) / middle, 4) if middle else None,
        }
    }


def combine(
    runs: list[Run],
    series_by: list[str] | None = None,
    baseline: str | None = None,
    validation: str = "structural",
) -> dict[str, Any]:
    """Build the combined comparison from loaded runs."""
    if not runs:
        raise ReportError("No reports to combine.")
    keys = default_series_keys(runs) if series_by is None else list(series_by)
    groups = group_series(runs, keys)
    names = series_names([labels for labels, _ in groups])
    columns = cohort_columns(runs)

    series: list[dict[str, Any]] = []
    members_by_name: dict[str, list[Run]] = {}
    for name, (labels, members) in zip(names, groups, strict=True):
        rows = [_run_row(run) for run in members]
        members_by_name[name] = members
        series.append(
            {
                "name": name,
                "labels": labels,
                "is_baseline": False,
                "run_indexes": [run.run_index for run in members],
                "files": [run.file for run in members],
                "model_ids": sorted({str(run.info.get("model_id")) for run in members}),
                "overall": summarize(
                    [request for run in members for request in run.timed], len(members)
                ),
                "cohorts": summarize_cohorts(members, columns),
                "runs": rows,
                "run_spread": _spread(_pooled_rates(rows)),
            }
        )

    base_index = pick_baseline(series, baseline)
    series.insert(0, series.pop(base_index))
    base = series[0]
    base["is_baseline"] = True
    base_hashes = [response_hashes(run) for run in members_by_name[base["name"]]]
    base_rates = _pooled_rates(base["runs"])

    for item in series:
        hashes = [response_hashes(run) for run in members_by_name[item["name"]]]
        item["identical_output"] = {
            "within_series": compare_outputs(hashes),
            "vs_baseline": None if item["is_baseline"] else compare_outputs(hashes, base_hashes),
        }
        if item["is_baseline"]:
            item["comparison"] = None
            continue
        comparison = compare_metrics(item["overall"], base["overall"])
        comparison["run_range_check"] = run_range_check(_pooled_rates(item["runs"]), base_rates)
        comparison["cohorts"] = {
            column: {
                value: compare_metrics(metrics, base["cohorts"][column][value])
                for value, metrics in values.items()
                if value in base["cohorts"].get(column, {})
            }
            for column, values in item["cohorts"].items()
        }
        item["comparison"] = comparison

    series_runs = [(item["name"], members_by_name[item["name"]]) for item in series]
    return {
        "combined_version": COMBINED_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "validation": validation,
        "series_by": keys or ["model_id"],
        "baseline": base["name"],
        "cohort_columns": columns,
        "series": series,
        "warnings": collect_warnings(series_runs, columns),
        "provenance": collect_provenance(series_runs),
        "inputs": [
            {
                "file": run.file,
                "sha256": run.file_sha256,
                "series": name,
                "run_index": run.run_index,
                "timestamp": run.info.get("timestamp"),
                "timed_request_count": len(run.timed),
            }
            for name, members in series_runs
            for run in members
        ],
    }


# =============================================================================
# CLI
# =============================================================================


def _format(value: float | None, digits: int = 1, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:.{digits}f}{suffix}"


def print_summary(combined: dict[str, Any], out: Any = None) -> None:
    """Plain-text summary of the combined result."""
    out = out or sys.stdout
    print(f"Baseline: {combined['baseline']}", file=out)
    print("", file=out)
    header = (
        f"{'Series':<32} {'Runs':>4} {'Requests':>8} {'Decode tok/s':>12} "
        f"{'Speedup':>8} {'TTFT p50 ms':>11}"
    )
    print(header, file=out)
    print("-" * len(header), file=out)
    for item in combined["series"]:
        overall = item["overall"]
        if item["is_baseline"]:
            speedup = "baseline"
        else:
            speedup = _format((item["comparison"] or {}).get("decode_speedup_pooled"), 2, "x")
        decode = _format(metric_field(overall, "decode_tokens_per_second", "pooled"))
        ttft = _format(metric_field(overall, "ttft_ms", "p50"))
        print(
            f"{item['name'][:32]:<32} {overall['run_count']:>4} {overall['request_count']:>8} "
            f"{decode:>12} {speedup:>8} {ttft:>11}",
            file=out,
        )
    if combined["warnings"]:
        print("", file=out)
        print("Warnings:", file=out)
        for warning in combined["warnings"]:
            print(f"  - [{warning['code']}] {warning['message']}", file=out)


def _split_keys(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    return [key.strip() for value in values for key in value.split(",") if key.strip()]


def build_parser() -> argparse.ArgumentParser:
    """Command-line options."""
    parser = argparse.ArgumentParser(
        description="Combine `winml perf --endpoint` reports into one comparison JSON file.",
    )
    parser.add_argument(
        "inputs", nargs="+", help="Report files, folders of *.json, or glob patterns."
    )
    parser.add_argument("-o", "--output", required=True, help="Combined JSON file to write.")
    parser.add_argument(
        "--series-by",
        action="append",
        metavar="KEY[,KEY]",
        help="Run-label keys that name a series (repeatable). Default: every run-label key "
        "except run-index-like keys such as 'run' or 'repeat'; the model id when no labels exist.",
    )
    parser.add_argument(
        "--baseline",
        metavar="KEY=VALUE[,KEY=VALUE] | NAME",
        help="Series to compare against, by run labels or series name. Default: the first series.",
    )
    parser.add_argument(
        "--schema",
        help="Report JSON Schema to validate against. Default: the copy in the winml-cli checkout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    try:
        paths = expand_inputs(args.inputs)
        validator = make_validator(find_schema(args.schema))
        runs = [load_run(path, validator) for path in paths]
        combined = combine(
            runs,
            series_by=_split_keys(args.series_by),
            baseline=args.baseline,
            validation="json-schema" if validator is not None else "structural",
        )
    except ReportError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(combined, indent=2) + "\n", encoding="utf-8")
    print(
        f"Combined {len(runs)} reports into {len(combined['series'])} series "
        f"({combined['validation']} validation)."
    )
    print_summary(combined)
    print("")
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
