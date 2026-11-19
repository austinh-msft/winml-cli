---
name: llm-perf-report
description: 'Use when turning `winml perf --endpoint` JSON reports into a shareable comparison: combining runs of several configurations (for example a target model without and with a speculative-decoding draft model, two server builds, or two quantizations) into one JSON summary, and rendering a PowerPoint deck with native charts for decode throughput, speedup against a baseline, per-cohort results (prompt category or input length), draft acceptance, time to first token, identical outputs, and provenance. Also use when a user asks how to read or share such a comparison, or why outputs differ between runs. Do not use for running the benchmark itself (use llm-speed-bench), for classic ONNX model latency (use use-winml-cli), or for accuracy evaluation.'
---

# Reports from `winml perf --endpoint`

`winml perf --endpoint` writes one self-contained JSON report per run. This
skill combines many reports into one comparison and renders it as a deck that
can be shared without the raw files:

1. [`scripts/combine.py`](scripts/combine.py) reads reports, groups them into
   series, pools the timed requests of each series, compares every series with a
   baseline, and writes one combined JSON file. It needs only the Python
   standard library; under `uv run` it also validates each report against the
   JSON Schema that ships with winml-cli.
2. [`scripts/render_slides.py`](scripts/render_slides.py) turns the combined
   JSON into a 16:9 PowerPoint deck with native, editable charts and tables.
   It declares `python-pptx` inline (PEP 723), so `uv run` installs it in an
   isolated environment. winml-cli itself does not depend on it.

To produce the reports, see the [`llm-speed-bench`](../llm-speed-bench/SKILL.md)
skill and the [OpenAI-compatible server sample](../../docs/samples/openai-endpoint-perf.md).

## Rules

- Never commit reports, combined JSON or decks. Keep them in a folder outside
  the repository, and share them from there.
- Never edit report files by hand. Rerun the benchmark instead.
- Present results as measured. A speedup below 1.00x is a valid result.
- Do not claim outputs are identical from a single comparison. Read the
  identical-output rate against the baseline's own run-to-run rate.
- Before sharing, check the deck's method slide: it lists the host OS, CPU and
  GPU names with driver versions, model file names and hashes, and the server
  fingerprint from the reports. Local folders and host names are never recorded.

## Inputs

Reports with `schema_version` 4 from `winml perf --endpoint`, ideally:

- one report per run, written with `-o`;
- `--run-label KEY=VALUE` naming each configuration, for example
  `--run-label config=plain` and `--run-label config=draft`;
- `--run-index N` numbering the repeats, with configurations interleaved
  (plain 0, draft 0, plain 1, draft 1, ...);
- the same prompts in every run (`--dataset`), so `dataset.prompts_sha256`
  matches, plus `--column cohort_column=...` for per-cohort results.

A cohort is a group of prompts that share the value of a dataset column, such
as `category` or the input-length `bucket` of SPEED-Bench.

## Step 1: Combine

Run from a winml-cli checkout:

```powershell
uv run skills\llm-perf-report\scripts\combine.py D:\bench\results -o D:\bench\report\combined.json --baseline config=plain
```

Inputs can be files, folders (every `*.json` inside) or glob patterns.

| Option | Meaning |
|---|---|
| `--baseline KEY=VALUE[,KEY=VALUE]` or `NAME` | The series to compare against. Default: the first series. |
| `--series-by KEY[,KEY]` | Run-label keys that name a series. Default: every run-label key except run-index-like keys (`run`, `repeat`, `iteration`, ...); the model id when the reports have no labels. |
| `--schema PATH` | Report JSON Schema. Default: the copy in the checkout; without it (or without `jsonschema`), only structural checks run. |

The script prints a summary table and any warnings, for example different
prompts, `max_new_tokens`, `extra_body`, temperature or hosts between runs,
mixed model ids or server fingerprints within a series, duplicate run indexes,
single-run series, and prompt-cache reuse in timed requests. Resolve warnings
before sharing, or call them out.

### Combined JSON (`combined_version` 1)

| Key | Contents |
|---|---|
| `baseline`, `series_by`, `cohort_columns`, `validation` | How the reports were grouped and checked. |
| `series[]` | One entry per series, baseline first: `name`, `labels`, `run_indexes`, `files`, `model_ids`, `overall` and `cohorts` metrics, `runs` (per-run pooled decode rate, TTFT p50, acceptance), `run_spread`, `comparison` (null for the baseline) and `identical_output`. |
| metrics | `request_count`, `run_count`, and summaries (`count`, `mean`, `std`, `min`, `max`, `p50`, `p90`) of `decode_tokens_per_second` (plus `pooled`), `ttft_ms`, `request_duration_ms`, `server_decode_tokens_per_second`, `tokens_per_event` (plus `pooled`), `prompt_tokens`, `completion_tokens`; `cached_prompt_tokens_total`; `draft` (pooled drafted and accepted tokens and `acceptance_rate`, or null); `finish_reason_counts`. |
| `comparison` | `decode_speedup_pooled`, `decode_speedup_mean`, `ttft_p50_ratio` (below 1 is faster), `run_range_check`, and the same ratios per cohort. |
| `identical_output` | `within_series` (every pair of runs in the series) and `vs_baseline` (every run against every baseline run), joined on prompt id and pass: `run_pairs`, `pairs`, `identical_pairs`, `rate`, `prompts`, `prompts_identical`. |
| `warnings[]` | `{code, message}`. |
| `provenance` | Date range (UTC), winml-cli versions, hosts, servers, model files (name, size, SHA-256), prompt sets, and settings, each with the series that used it. |
| `inputs[]` | File name (never the folder), SHA-256, series, run index, timestamp and timed request count of each report. |

Definitions:

- Decode tokens per second excludes the first token of each response. `pooled`
  divides all decode tokens by all decode time, so long responses weigh more;
  `mean` averages the per-request rates.
- Speedup is the series' pooled decode rate divided by the baseline's.
- `run_range_check` is `faster_in_every_run` or `slower_in_every_run` when the
  per-run pooled rates of the series and the baseline do not overlap,
  `overlaps_baseline_runs` when they do, and `not_enough_runs` with fewer than
  2 runs on either side.

## Step 2: Render the deck

```powershell
uv run skills\llm-perf-report\scripts\render_slides.py D:\bench\report\combined.json -o D:\bench\report\deck.pptx
```

Options: `--title TEXT` and `--max-table-rows N` (rows per table slide before
it continues on the next slide). The script prints the slide titles.

Slides, in order (charts and tables are native and editable):

1. Title: model, prompts, series and baseline, measurement dates.
2. Summary table: runs, requests, pooled decode rate, speedup, run-range check,
   TTFT p50, draft acceptance (when present), identical outputs.
3. Decode tokens per second by series, pooled and mean.
4. Decode tokens per second by run (with 2 or more runs), with the spread.
5. Decode speedup against the baseline, overall and per cohort column, drawn
   from the 1.00x line.
6. Decode tokens per second per cohort column.
7. Draft acceptance rate per cohort column, and a speculative-decoding table
   with tokens per streamed event and server decode rate (when present).
8. Time to first token by series (p50, p90) and p50 per cohort column.
9. Identical outputs within each series and against the baseline.
10. Method and provenance.
11. Caveats, including every warning from `combine.py`.

Every bar has a data label and every chart a legend, so no value depends on
color alone. Speaker notes explain how to read each chart.

## How to read the deck

- Look at the run-to-run spread first. A difference smaller than the spread of
  either series is noise; get at least 3 interleaved runs per configuration.
- Prefer the pooled rate for throughput and the per-cohort charts for where a
  configuration helps or hurts. Draft acceptance usually varies by cohort, so
  read it per cohort rather than overall.
- The within-series identical-output rate is the determinism of the setup.
  Greedy decoding on a server is not always reproducible across runs. A
  vs-baseline rate close to the baseline's within-series rate shows no added
  change in outputs; a much lower rate shows the configuration changed outputs.
- TTFT includes HTTP overhead and prefill, and drops when the server reuses a
  cached prompt; check the prompt-cache warning.

## Sharing

Copy the deck (and, when the audience wants numbers, `combined.json`) to the
shared location. Keep the raw reports next to them so anyone can rerun
`combine.py`. Do not add any of these files to a repository.

## Caveats

- Times are measured by the client from the HTTP stream.
- Requests are sent one at a time; concurrent load is not measured.
- Run the server on the measuring machine, and one large model at a time.
- Draft counts come from server timings, which not every server reports.
