# /// script
# requires-python = ">=3.10"
# dependencies = ["python-pptx>=1.0.2"]
# ///
# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------
"""Render a combined `winml perf --endpoint` comparison as a PowerPoint deck.

Input is the JSON written by `combine.py` (`combined_version` 1). Output is a
16:9 PPTX with native, editable charts and tables: a summary, decode
throughput and speedup against the baseline (overall and per cohort), draft
acceptance when present, time to first token, identical outputs, method and
provenance, and caveats.

`build_slides` decides what goes on each slide and needs only the standard
library; `write_pptx` draws those slides with python-pptx.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SUPPORTED_COMBINED_VERSION = 1
MAX_TABLE_ROWS = 12
MAX_BULLETS = 9

RUN_RANGE_TEXT = {
    "faster_in_every_run": "Faster in every run",
    "slower_in_every_run": "Slower in every run",
    "overlaps_baseline_runs": "Overlaps baseline runs",
    "not_enough_runs": "Needs 2 or more runs each",
}

DECIMAL = "0.0"
RATIO = '0.00"x"'
PERCENT = "0%"


# =============================================================================
# Formatting
# =============================================================================


def fmt_number(value: float | None, digits: int = 1) -> str:
    """Fixed-point number, or n/a."""
    return "n/a" if value is None else f"{value:,.{digits}f}"


def fmt_ratio(value: float | None) -> str:
    """Ratio such as 1.23x, or n/a."""
    return "n/a" if value is None else f"{value:.2f}x"


def fmt_percent(value: float | None) -> str:
    """Share such as 75.9%, or n/a."""
    return "n/a" if value is None else f"{value * 100:.1f}%"


def fmt_identical(block: dict[str, Any] | None) -> str:
    """Identical-output rate with its counts, for example 62.5% (5 of 8)."""
    if not block:
        return "n/a"
    return f"{fmt_percent(block['rate'])} ({block['identical_pairs']} of {block['pairs']})"


def fmt_bytes(size: int | None) -> str:
    """File size in MiB or GiB."""
    if size is None:
        return "size unknown"
    if size >= 1024**3:
        return f"{size / 1024**3:.2f} GiB"
    if size >= 1024**2:
        return f"{size / 1024**2:.1f} MiB"
    if size >= 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size} bytes"


def origin_axis_bounds(values: list[float], origin: float) -> tuple[float, float, float]:
    """Axis minimum, maximum and major unit that show `origin` and every value."""
    low = min([origin, *values])
    high = max([origin, *values])
    pad = max((high - low) * 0.12, 0.05)
    span = high - low + 2 * pad
    unit = next(
        (step for step in (0.05, 0.1, 0.2, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0) if span / step <= 10),
        20.0,
    )
    minimum = max(0.0, math.floor((low - pad) / unit) * unit)
    maximum = math.ceil((high + pad) / unit) * unit
    return round(minimum, 6), round(maximum, 6), unit


def _field(metrics: dict[str, Any] | None, metric: str, key: str) -> float | None:
    return ((metrics or {}).get(metric) or {}).get(key)


def _acceptance(metrics: dict[str, Any] | None) -> float | None:
    return ((metrics or {}).get("draft") or {}).get("acceptance_rate")


def _short_hash(value: str | None) -> str:
    return "n/a" if not value else value[:12]


def _date_range(provenance: dict[str, Any]) -> str:
    dates = provenance.get("date_range")
    if not dates:
        return "n/a"
    first = datetime.fromisoformat(dates["first"])
    last = datetime.fromisoformat(dates["last"])
    if first.date() == last.date():
        return f"{first:%Y-%m-%d %H:%M} to {last:%H:%M} UTC"
    return f"{first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M} UTC"


# =============================================================================
# Slide content model
# =============================================================================


def _chart(
    title: str,
    categories: list[str],
    series: list[dict[str, Any]],
    number_format: str,
    value_axis_title: str,
    category_axis_title: str | None = None,
    footnote: str | None = None,
    notes: str | None = None,
    origin: float | None = None,
) -> dict[str, Any]:
    return {
        "layout": "chart",
        "title": title,
        "chart": {
            "kind": "clustered_column",
            "categories": categories,
            "series": series,
            "number_format": number_format,
            "value_axis_title": value_axis_title,
            "category_axis_title": category_axis_title,
            "origin": origin,
        },
        "footnote": footnote,
        "notes": notes,
    }


def _tables(
    title: str,
    columns: list[str],
    rows: list[list[str]],
    footnote: str | None = None,
    notes: str | None = None,
    max_rows: int = MAX_TABLE_ROWS,
) -> list[dict[str, Any]]:
    """One table slide, or several when the rows do not fit."""
    pages = [rows[start : start + max_rows] for start in range(0, len(rows), max_rows)] or [[]]
    slides = []
    for number, page in enumerate(pages, start=1):
        suffix = f" ({number} of {len(pages)})" if len(pages) > 1 else ""
        slides.append(
            {
                "layout": "table",
                "title": title + suffix,
                "columns": columns,
                "rows": page,
                "footnote": footnote,
                "notes": notes,
            }
        )
    return slides


def _bullets(title: str, bullets: list[str], notes: str | None = None) -> list[dict[str, Any]]:
    pages = [bullets[start : start + MAX_BULLETS] for start in range(0, len(bullets), MAX_BULLETS)]
    return [
        {
            "layout": "bullets",
            "title": title + (f" ({number} of {len(pages)})" if len(pages) > 1 else ""),
            "bullets": page,
            "footnote": None,
            "notes": notes,
        }
        for number, page in enumerate(pages, start=1)
    ]


def _title_slide(combined: dict[str, Any], title: str | None) -> dict[str, Any]:
    provenance = combined["provenance"]
    models = sorted({model for item in combined["series"] for model in item["model_ids"]})
    datasets = [entry for entry in provenance["datasets"] if entry.get("source") != "single prompt"]
    lines = [f"Model: {', '.join(models)}"]
    if datasets:
        dataset = datasets[0]
        lines.append(
            f"Prompts: {dataset.get('path')} ({dataset.get('selected_row_count')} prompts)"
        )
    else:
        lines.append("Prompts: single prompt")
    series = ", ".join(item["name"] for item in combined["series"])
    lines.append(f"Series: {series} (baseline: {combined['baseline']})")
    lines.append(f"Measured: {_date_range(provenance)}")
    return {
        "layout": "title",
        "title": title or "Text generation speed report",
        "subtitle": "\n".join(lines),
        "notes": "Measured with winml perf --endpoint against an OpenAI-compatible server. "
        "Combined with the llm-perf-report skill.",
    }


def _summary(combined: dict[str, Any], max_rows: int) -> list[dict[str, Any]]:
    has_draft = any(_acceptance(item["overall"]) is not None for item in combined["series"])
    columns = [
        "Series",
        "Runs",
        "Requests",
        "Decode tokens/s (pooled)",
        "Speedup vs baseline",
        "Run-range check",
        "TTFT p50 (ms)",
    ]
    if has_draft:
        columns.append("Draft acceptance")
    columns.append("Identical output vs baseline")
    rows = []
    for item in combined["series"]:
        overall = item["overall"]
        comparison = item["comparison"] or {}
        row = [
            item["name"] + (" (baseline)" if item["is_baseline"] else ""),
            str(overall["run_count"]),
            str(overall["request_count"]),
            fmt_number(_field(overall, "decode_tokens_per_second", "pooled")),
            "1.00x (baseline)"
            if item["is_baseline"]
            else fmt_ratio(comparison.get("decode_speedup_pooled")),
            "Baseline"
            if item["is_baseline"]
            else RUN_RANGE_TEXT.get(comparison.get("run_range_check"), "n/a"),
            fmt_number(_field(overall, "ttft_ms", "p50")),
        ]
        if has_draft:
            row.append(fmt_percent(_acceptance(overall)))
        identical = item["identical_output"]
        row.append(
            "Within baseline: " + fmt_identical(identical["within_series"])
            if item["is_baseline"]
            else fmt_identical(identical["vs_baseline"])
        )
        rows.append(row)
    footnote = (
        "Speedup is pooled decode tokens per second divided by the baseline's; above 1.00x is "
        "faster, "
        "below is slower. Run-range check compares the per-run pooled rates of each series with "
        "the "
        "baseline's."
    )
    notes = (
        "Pooled decode tokens per second divides all decode tokens (after the first token of each "
        "response) by all decode time. Faster or slower in every run means the ranges of per-run "
        "rates "
        "do not overlap; overlapping ranges mean the difference is within run-to-run noise."
    )
    return _tables("Summary", columns, rows, footnote, notes, max_rows)


def _decode_slides(combined: dict[str, Any]) -> list[dict[str, Any]]:
    series = combined["series"]
    names = [item["name"] for item in series]
    slides = [
        _chart(
            "Decode tokens per second by series",
            names,
            [
                {
                    "name": "Pooled over requests",
                    "values": [
                        _field(item["overall"], "decode_tokens_per_second", "pooled")
                        for item in series
                    ],
                },
                {
                    "name": "Mean of per-request rates",
                    "values": [
                        _field(item["overall"], "decode_tokens_per_second", "mean")
                        for item in series
                    ],
                },
            ],
            DECIMAL,
            "Tokens per second",
            footnote="Higher is faster. Pooled weighs long responses more than the mean of "
            "per-request rates.",
            notes="Client-side decode rate: tokens after the first one, divided by the time "
            "between the first "
            "and last streamed token.",
        )
    ]
    if any(len(item["runs"]) > 1 for item in series):
        indexes: list[Any] = []
        for item in series:
            indexes.extend(
                row["run_index"] for row in item["runs"] if row["run_index"] not in indexes
            )
        chart_series = []
        for index in indexes:
            values = []
            for item in series:
                rates = [
                    row["decode_tokens_per_second_pooled"]
                    for row in item["runs"]
                    if row["run_index"] == index
                ]
                values.append(rates[0] if rates else None)
            label = "Run (no index)" if index is None else f"Run {index}"
            chart_series.append({"name": label, "values": values})
        spreads = []
        for item in series:
            spread = (item.get("run_spread") or {}).get("decode_tokens_per_second_pooled")
            if spread and spread.get("relative_range") is not None:
                spreads.append(f"{item['name']}: {fmt_percent(spread['relative_range'])}")
        slides.append(
            _chart(
                "Decode tokens per second by run",
                names,
                chart_series,
                DECIMAL,
                "Tokens per second",
                footnote="Each bar is one run. Spread (max minus min, over median): "
                + "; ".join(spreads)
                + ".",
                notes="Run-to-run noise. Differences between series smaller than this spread are "
                "not reliable.",
            )
        )
    return slides


def _speedup_slides(combined: dict[str, Any]) -> list[dict[str, Any]]:
    others = [item for item in combined["series"] if not item["is_baseline"]]
    if not others:
        return []
    baseline = combined["baseline"]
    footnote = "1.00x equals the baseline. Bars above the line are faster; bars below are slower."
    slides = [
        _chart(
            f"Decode speedup vs {baseline}",
            [item["name"] for item in others],
            [
                {
                    "name": "Pooled",
                    "values": [item["comparison"]["decode_speedup_pooled"] for item in others],
                },
                {
                    "name": "Mean of requests",
                    "values": [item["comparison"]["decode_speedup_mean"] for item in others],
                },
            ],
            RATIO,
            "Speedup (x)",
            footnote=footnote,
            notes="Ratio of decode tokens per second to the baseline's, over all timed requests.",
            origin=1.0,
        )
    ]
    base = combined["series"][0]
    for column, values in base["cohorts"].items():
        categories = list(values)
        slides.append(
            _chart(
                f"Decode speedup vs {baseline} by {column}",
                categories,
                [
                    {
                        "name": item["name"],
                        "values": [
                            (
                                (item["comparison"]["cohorts"].get(column) or {}).get(value) or {}
                            ).get("decode_speedup_pooled")
                            for value in categories
                        ],
                    }
                    for item in others
                ],
                RATIO,
                "Speedup (x)",
                category_axis_title=column,
                footnote=footnote,
                notes=f"Pooled decode speedup for each {column} value, against the same {column} "
                "in the baseline.",
                origin=1.0,
            )
        )
    return slides


def _cohort_values(combined: dict[str, Any], column: str) -> list[str]:
    values: list[str] = []
    for item in combined["series"]:
        values.extend(value for value in item["cohorts"].get(column, {}) if value not in values)
    return values


def _cohort_metric_charts(
    combined: dict[str, Any],
    title: str,
    getter: Any,
    number_format: str,
    axis: str,
    series_filter: Any = None,
    footnote: str | None = None,
    notes: str | None = None,
) -> list[dict[str, Any]]:
    series = [item for item in combined["series"] if series_filter is None or series_filter(item)]
    slides = []
    for column in combined["cohort_columns"]:
        categories = _cohort_values(combined, column)
        if not categories:
            continue
        slides.append(
            _chart(
                f"{title} by {column}",
                categories,
                [
                    {
                        "name": item["name"],
                        "values": [
                            getter(item["cohorts"].get(column, {}).get(value))
                            for value in categories
                        ],
                    }
                    for item in series
                ],
                number_format,
                axis,
                category_axis_title=column,
                footnote=footnote,
                notes=notes,
            )
        )
    return slides


def _draft_slides(combined: dict[str, Any], max_rows: int) -> list[dict[str, Any]]:
    series = combined["series"]
    drafted = [item for item in series if _acceptance(item["overall"]) is not None]
    batched = any(
        (_field(item["overall"], "tokens_per_event", "pooled") or 1.0) > 1.0 for item in series
    )
    if not drafted and not batched:
        return []
    slides: list[dict[str, Any]] = []
    if drafted:
        slides.extend(
            _cohort_metric_charts(
                combined,
                "Draft acceptance rate",
                _acceptance,
                PERCENT,
                "Accepted share of drafted tokens",
                series_filter=lambda item: _acceptance(item["overall"]) is not None,
                footnote="Pooled over requests: accepted draft tokens divided by drafted tokens, "
                "as the server "
                "reports them.",
            )
        )
    rows = []
    for item in series:
        overall = item["overall"]
        draft = overall.get("draft") or {}
        rows.append(
            [
                item["name"],
                fmt_number(draft.get("draft_tokens"), 0) if draft else "n/a",
                fmt_number(draft.get("draft_tokens_accepted"), 0) if draft else "n/a",
                fmt_percent(draft.get("acceptance_rate")) if draft else "n/a",
                fmt_number(_field(overall, "tokens_per_event", "pooled"), 2),
                fmt_number(_field(overall, "server_decode_tokens_per_second", "mean")),
            ]
        )
    slides.extend(
        _tables(
            "Speculative decoding",
            [
                "Series",
                "Drafted tokens",
                "Accepted tokens",
                "Acceptance rate",
                "Tokens per streamed event",
                "Server decode tokens/s (mean)",
            ],
            rows,
            footnote="Draft counts come from the server's timings. Tokens per streamed event "
            "above 1 means the "
            "server sent several tokens in one event.",
            max_rows=max_rows,
        )
    )
    return slides


def _ttft_slides(combined: dict[str, Any]) -> list[dict[str, Any]]:
    series = combined["series"]
    footnote = "Lower is faster. Measured from sending the request to the first streamed token."
    slides = [
        _chart(
            "Time to first token by series",
            [item["name"] for item in series],
            [
                {
                    "name": "p50",
                    "values": [_field(item["overall"], "ttft_ms", "p50") for item in series],
                },
                {
                    "name": "p90",
                    "values": [_field(item["overall"], "ttft_ms", "p90") for item in series],
                },
            ],
            DECIMAL,
            "Milliseconds",
            footnote=footnote,
            notes="TTFT includes HTTP overhead and prefill. It depends on prompt length and on "
            "prompt caching.",
        )
    ]
    slides.extend(
        _cohort_metric_charts(
            combined,
            "Time to first token (p50)",
            lambda metrics: _field(metrics, "ttft_ms", "p50"),
            DECIMAL,
            "Milliseconds",
            footnote=footnote,
        )
    )
    return slides


def _identical_slides(combined: dict[str, Any], max_rows: int) -> list[dict[str, Any]]:
    rows = []
    for item in combined["series"]:
        within = item["identical_output"]["within_series"]
        versus = item["identical_output"]["vs_baseline"]
        rows.append(
            [
                item["name"] + (" (baseline)" if item["is_baseline"] else ""),
                str(len(item["runs"])),
                fmt_identical(within) if within else "n/a (one run)",
                f"{within['prompts_identical']} of {within['prompts']}" if within else "n/a",
                "Baseline" if item["is_baseline"] else fmt_identical(versus),
                "Baseline"
                if item["is_baseline"] or not versus
                else f"{versus['prompts_identical']} of {versus['prompts']}",
            ]
        )
    return _tables(
        "Identical outputs",
        [
            "Series",
            "Runs",
            "Within series (run pairs)",
            "Prompts identical in every run",
            "Vs baseline (run pairs)",
            "Prompts identical with baseline",
        ],
        rows,
        footnote="Responses are compared by SHA-256 per prompt and pass. The within-series rate "
        "is the "
        "determinism of the setup; a vs-baseline rate near it shows no added change in outputs.",
        notes="Greedy decoding on a server is not always reproducible: batching, kernels and "
        "caching can change "
        "outputs between runs of the same configuration. Read the vs-baseline rate against the "
        "baseline's "
        "within-series rate, not against 100%.",
        max_rows=max_rows,
    )


def _provenance_rows(combined: dict[str, Any]) -> list[list[str]]:
    provenance = combined["provenance"]

    def scope(entry: dict[str, Any]) -> str:
        if len(entry["series"]) == len(combined["series"]):
            return ""
        return f" [{', '.join(entry['series'])}]"

    inputs = combined["inputs"]
    files = ", ".join(entry["file"] for entry in inputs[:8]) + (", ..." if len(inputs) > 8 else "")
    rows = [
        ["Measured", _date_range(provenance)],
        ["Tool", f"winml perf --endpoint; winml-cli {', '.join(provenance['winml_cli_versions'])}"],
        ["Reports", f"{len(inputs)} ({files}); {combined['validation']} validation"],
        [
            "Series",
            f"Grouped by run labels {', '.join(combined['series_by'])}; baseline "
            f"{combined['baseline']}",
        ],
    ]
    rows.extend(
        [
            "Server",
            f"model {entry.get('model_id')}; fingerprint "
            f"{entry.get('system_fingerprint') or 'n/a'}" + scope(entry),
        ]
        for entry in provenance["servers"]
    )
    rows.extend(
        [
            f"File ({entry.get('label')})",
            f"{entry.get('name')}, {fmt_bytes(entry.get('size_bytes'))}, sha256 "
            f"{_short_hash(entry.get('sha256'))}" + scope(entry),
        ]
        for entry in provenance["files"]
    )
    for entry in provenance["datasets"]:
        if entry.get("source") == "single prompt":
            text = "single prompt"
        else:
            parts = [f"{entry.get('source')} {entry.get('path')}"]
            parts.extend(
                f"{key} {entry[key]}" for key in ("name", "revision", "split") if entry.get(key)
            )
            parts.append(f"{entry.get('selected_row_count')} prompts")
            if entry.get("cohort_columns"):
                parts.append(f"cohorts {', '.join(entry['cohort_columns'])}")
            parts.append(f"prompts sha256 {_short_hash(entry.get('prompts_sha256'))}")
            text = "; ".join(parts)
        rows.append(["Prompts", text + scope(entry)])
    for entry in provenance["settings"]:
        extra = json.dumps(entry.get("extra_body") or {}, sort_keys=True)
        rows.append(
            [
                "Settings",
                f"max_new_tokens {entry.get('max_new_tokens')}; iterations "
                f"{entry.get('iterations')}; "
                f"warmup {entry.get('warmup')}; temperature {entry.get('temperature')}; "
                f"extra_body {extra}" + scope(entry),
            ]
        )
    for entry in provenance["hosts"]:
        gpus = "; ".join(
            f"{gpu.get('name')} (driver {gpu.get('driver_version') or 'n/a'})"
            for gpu in entry.get("gpus") or []
        )
        ram = entry.get("ram_mib")
        rows.append(
            [
                "Host",
                f"{entry.get('os') or 'OS n/a'}; CPU "
                f"{', '.join(filter(None, entry.get('cpus') or [])) or 'n/a'}; "
                f"GPU {gpus or 'n/a'}; RAM {fmt_number(ram / 1024, 0) + ' GiB' if ram else 'n/a'}"
                + scope(entry),
            ]
        )
    return rows


def _caveats(combined: dict[str, Any]) -> list[dict[str, Any]]:
    runs = sorted({len(item["runs"]) for item in combined["series"]})
    base = combined["series"][0]
    within = base["identical_output"]["within_series"]
    bullets = [
        "Times are measured by the client from the HTTP stream, so they include HTTP and network "
        "overhead.",
        "Requests are sent one at a time; throughput under concurrent load is not measured.",
        "Decode tokens per second excludes each response's first token. Responses of different "
        "lengths "
        "weigh differently in pooled and mean rates.",
        f"Runs per series: {', '.join(map(str, runs))}. Differences inside the run-to-run spread "
        "are noise; "
        "use 3 or more interleaved runs per configuration.",
    ]
    if within and within["identical_pairs"] < within["pairs"]:
        bullets.append(
            f"Outputs are not fully reproducible: {fmt_identical(within)} of the baseline's "
            "compared "
            "responses matched between its runs. Judge other series against that rate, not "
            "against 100%."
        )
    elif within:
        bullets.append(
            f"The baseline's runs gave identical responses ({fmt_identical(within)}). That holds "
            "for these "
            "runs only; a single comparison never proves outputs are always identical."
        )
    else:
        bullets.append(
            "The baseline has one run, so the determinism of its outputs is unknown; one "
            "comparison never "
            "proves outputs are identical."
        )
    if len(combined["series"]) > 1:
        bullets.append(
            "A speedup below 1.00x is a valid result: the configuration was slower than the "
            "baseline."
        )
    warnings = combined.get("warnings") or []
    if not any(warning["code"] == "prompt_cache" for warning in warnings):
        bullets.append("Prompt caching was not observed in the timed requests.")
    bullets.extend(f"Warning ({warning['code']}): {warning['message']}" for warning in warnings)
    return _bullets("Caveats", bullets, notes="Warnings come from combine.py.")


def build_slides(
    combined: dict[str, Any], title: str | None = None, max_table_rows: int = MAX_TABLE_ROWS
) -> list[dict[str, Any]]:
    """The content of each slide, in order, without drawing anything."""
    version = combined.get("combined_version")
    if version != SUPPORTED_COMBINED_VERSION:
        raise ValueError(
            f"combined_version {version!r} is not supported; expected {SUPPORTED_COMBINED_VERSION}"
        )
    slides = [_title_slide(combined, title)]
    slides.extend(_summary(combined, max_table_rows))
    slides.extend(_decode_slides(combined))
    slides.extend(_speedup_slides(combined))
    slides.extend(
        _cohort_metric_charts(
            combined,
            "Decode tokens per second",
            lambda metrics: _field(metrics, "decode_tokens_per_second", "pooled"),
            DECIMAL,
            "Tokens per second (pooled)",
            footnote="Higher is faster.",
        )
    )
    slides.extend(_draft_slides(combined, max_table_rows))
    slides.extend(_ttft_slides(combined))
    slides.extend(_identical_slides(combined, max_table_rows))
    slides.extend(
        _tables(
            "Method and provenance",
            ["Item", "Details"],
            _provenance_rows(combined),
            footnote="Local folders are not recorded. File hashes identify the exact model files.",
            max_rows=max_table_rows,
        )
    )
    slides.extend(_caveats(combined))
    return slides


# =============================================================================
# PowerPoint writer
# =============================================================================

SLIDE_WIDTH_IN = 13.333
SLIDE_HEIGHT_IN = 7.5
MARGIN_IN = 0.5
TITLE_TOP_IN = 0.3
TITLE_HEIGHT_IN = 0.9
BODY_TOP_IN = 1.3
FOOTNOTE_TOP_IN = 6.7
FOOTNOTE_HEIGHT_IN = 0.6


def _set_alt_text(shape: Any, text: str) -> None:
    shape._element.xpath("./*[1]/p:cNvPr")[0].set("descr", text)


def _add_footnote(slide: Any, text: str | None, inches: Any, points: Any) -> None:
    if not text:
        return
    box = slide.shapes.add_textbox(
        inches(MARGIN_IN),
        inches(FOOTNOTE_TOP_IN),
        inches(SLIDE_WIDTH_IN - 2 * MARGIN_IN),
        inches(FOOTNOTE_HEIGHT_IN),
    )
    frame = box.text_frame
    frame.word_wrap = True
    frame.text = text
    for paragraph in frame.paragraphs:
        for run in paragraph.runs:
            run.font.size = points(11)


def _set_title(slide: Any, text: str, inches: Any, points: Any) -> None:
    shape = slide.shapes.title
    shape.left = inches(MARGIN_IN)
    shape.top = inches(TITLE_TOP_IN)
    shape.width = inches(SLIDE_WIDTH_IN - 2 * MARGIN_IN)
    shape.height = inches(TITLE_HEIGHT_IN)
    shape.text = text
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            run.font.size = points(30)


def _column_widths(columns: list[str], rows: list[list[str]], total: float) -> list[float]:
    weights = []
    for index, column in enumerate(columns):
        header_word = max(len(word) for word in column.split()) + 2
        longest = max([header_word] + [len(row[index]) for row in rows])
        weights.append(min(max(longest, 7), 90))
    scale = total / sum(weights)
    return [weight * scale for weight in weights]


def _draw_table(slide: Any, spec: dict[str, Any], inches: Any, points: Any) -> None:
    columns, rows = spec["columns"], spec["rows"]
    width = SLIDE_WIDTH_IN - 2 * MARGIN_IN
    height = min(0.4 * (len(rows) + 1), FOOTNOTE_TOP_IN - BODY_TOP_IN - 0.1)
    shape = slide.shapes.add_table(
        len(rows) + 1,
        len(columns),
        inches(MARGIN_IN),
        inches(BODY_TOP_IN),
        inches(width),
        inches(height),
    )
    _set_alt_text(shape, f"Table: {spec['title']}")
    table = shape.table
    for index, column_width in enumerate(_column_widths(columns, rows, width)):
        table.columns[index].width = inches(column_width)
    size = 12 if len(columns) <= 6 else 11
    for row_index, values in enumerate([columns, *rows]):
        for column_index, value in enumerate(values):
            cell = table.cell(row_index, column_index)
            cell.text = value
            for paragraph in cell.text_frame.paragraphs:
                for run in paragraph.runs:
                    run.font.size = points(size)
                    run.font.bold = row_index == 0


def _draw_chart(slide: Any, spec: dict[str, Any], inches: Any, points: Any) -> None:
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import (
        XL_CHART_TYPE,
        XL_LABEL_POSITION,
        XL_LEGEND_POSITION,
        XL_TICK_LABEL_POSITION,
    )

    chart_spec = spec["chart"]
    data = CategoryChartData(number_format=chart_spec["number_format"])
    data.categories = chart_spec["categories"]
    for series in chart_spec["series"]:
        data.add_series(series["name"], series["values"])
    frame = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED,
        inches(MARGIN_IN),
        inches(BODY_TOP_IN),
        inches(SLIDE_WIDTH_IN - 2 * MARGIN_IN),
        inches(FOOTNOTE_TOP_IN - BODY_TOP_IN - 0.1),
        data,
    )
    names = ", ".join(series["name"] for series in chart_spec["series"])
    _set_alt_text(
        frame,
        f"Column chart: {spec['title']}. Categories: {', '.join(chart_spec['categories'])}. "
        f"Series: {names}.",
    )
    chart = frame.chart
    chart.has_title = False
    chart.font.size = points(12)
    chart.has_legend = True
    chart.legend.position = XL_LEGEND_POSITION.BOTTOM
    chart.legend.include_in_layout = False
    points_count = len(chart_spec["categories"]) * len(chart_spec["series"])
    plot = chart.plots[0]
    plot.gap_width = 80
    plot.vary_by_categories = False
    for series in plot.series:
        series.invert_if_negative = False
    plot.has_data_labels = True
    labels = plot.data_labels
    labels.number_format = chart_spec["number_format"]
    labels.number_format_is_linked = False
    labels.position = XL_LABEL_POSITION.OUTSIDE_END
    labels.font.size = points(11 if points_count <= 16 else 9 if points_count <= 36 else 7)
    value_axis = chart.value_axis
    value_axis.has_major_gridlines = True
    value_axis.tick_labels.number_format = chart_spec["number_format"]
    value_axis.tick_labels.number_format_is_linked = False
    value_axis.has_title = True
    value_axis.axis_title.text_frame.text = chart_spec["value_axis_title"]
    category_axis = chart.category_axis
    category_axis.tick_label_position = XL_TICK_LABEL_POSITION.LOW
    origin = chart_spec.get("origin")
    if origin is not None:
        values = [
            value
            for series in chart_spec["series"]
            for value in series["values"]
            if value is not None
        ]
        low, high, unit = origin_axis_bounds(values, origin)
        value_axis.minimum_scale = low
        value_axis.maximum_scale = high
        value_axis.major_unit = unit
        value_axis.crosses_at = origin
    if chart_spec.get("category_axis_title"):
        category_axis.has_title = True
        category_axis.axis_title.text_frame.text = chart_spec["category_axis_title"]


def _draw_bullets(slide: Any, spec: dict[str, Any], inches: Any, points: Any) -> None:
    box = slide.shapes.add_textbox(
        inches(MARGIN_IN),
        inches(BODY_TOP_IN),
        inches(SLIDE_WIDTH_IN - 2 * MARGIN_IN),
        inches(FOOTNOTE_TOP_IN - BODY_TOP_IN),
    )
    frame = box.text_frame
    frame.word_wrap = True
    for index, text in enumerate(spec["bullets"]):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.text = f"- {text}"
        paragraph.space_after = points(8)
        for run in paragraph.runs:
            run.font.size = points(16 if len(spec["bullets"]) <= 6 else 14)


def write_pptx(slides: list[dict[str, Any]], output: Path, title: str | None = None) -> None:
    """Draw the slides into a 16:9 PPTX file."""
    from pptx import Presentation
    from pptx.util import Inches, Pt

    presentation = Presentation()
    presentation.slide_width = Inches(SLIDE_WIDTH_IN)
    presentation.slide_height = Inches(SLIDE_HEIGHT_IN)
    title_layout = presentation.slide_layouts[0]
    title_only_layout = presentation.slide_layouts[5]
    for spec in slides:
        if spec["layout"] == "title":
            slide = presentation.slides.add_slide(title_layout)
            _set_title(slide, spec["title"], Inches, Pt)
            slide.shapes.title.top = Inches(1.6)
            slide.shapes.title.height = Inches(1.4)
            subtitle = slide.placeholders[1]
            subtitle.left = Inches(MARGIN_IN)
            subtitle.top = Inches(3.2)
            subtitle.width = Inches(SLIDE_WIDTH_IN - 2 * MARGIN_IN)
            subtitle.height = Inches(3.2)
            subtitle.text = spec["subtitle"]
            for paragraph in subtitle.text_frame.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(18)
        else:
            slide = presentation.slides.add_slide(title_only_layout)
            _set_title(slide, spec["title"], Inches, Pt)
            draw = {"chart": _draw_chart, "table": _draw_table, "bullets": _draw_bullets}[
                spec["layout"]
            ]
            draw(slide, spec, Inches, Pt)
            _add_footnote(slide, spec.get("footnote"), Inches, Pt)
        if spec.get("notes"):
            slide.notes_slide.notes_text_frame.text = spec["notes"]
    presentation.core_properties.title = title or slides[0]["title"]
    output.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(str(output))


# =============================================================================
# CLI
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    """Command-line options."""
    parser = argparse.ArgumentParser(
        description="Render a combined comparison (from combine.py) as a PPTX deck."
    )
    parser.add_argument("combined", help="Combined JSON written by combine.py.")
    parser.add_argument("-o", "--output", required=True, help="PPTX file to write.")
    parser.add_argument("--title", help="Deck title. Default: 'Text generation speed report'.")
    parser.add_argument(
        "--max-table-rows",
        type=int,
        default=MAX_TABLE_ROWS,
        help="Rows per table slide before continuing on the next slide (default "
        f"{MAX_TABLE_ROWS}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    try:
        combined = json.loads(Path(args.combined).read_text(encoding="utf-8"))
        slides = build_slides(combined, args.title, args.max_table_rows)
    except (OSError, ValueError, KeyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    output = Path(args.output)
    write_pptx(slides, output, args.title)
    print(f"Wrote {len(slides)} slides to {output}")
    for number, slide in enumerate(slides, start=1):
        print(f"  {number:>2}. {slide['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
