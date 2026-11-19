# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------

import json

import pytest


def runs_from(combine_module, reports, tmp_path):
    folder = tmp_path / "reports"
    folder.mkdir(exist_ok=True)
    runs = []
    for name, report in reports.items():
        path = folder / name
        path.write_text(json.dumps(report), encoding="utf-8")
        runs.append(combine_module.load_run(path))
    return runs


def by_name(combined):
    return {item["name"]: item for item in combined["series"]}


def warning_codes(combined):
    return {warning["code"] for warning in combined["warnings"]}


def test_synthetic_reports_match_report_schema(combine_module, plain_and_draft_reports):
    validator = combine_module.make_validator(combine_module.find_schema())
    assert validator is not None
    for name, report in plain_and_draft_reports.items():
        combine_module.check_report(report, name, validator)


def test_series_default_to_varying_labels_and_baseline_comes_first(
    combine_module, plain_and_draft_reports, tmp_path
):
    runs = runs_from(combine_module, plain_and_draft_reports, tmp_path)
    combined = combine_module.combine(runs, baseline="config=plain")
    assert combined["combined_version"] == 1
    assert combined["series_by"] == ["server", "config"]
    assert [item["name"] for item in combined["series"]] == ["config=plain", "config=draft"]
    plain = combined["series"][0]
    assert plain["is_baseline"] and plain["comparison"] is None
    assert plain["labels"] == {"server": "llama.cpp", "config": "plain"}
    assert plain["run_indexes"] == [0, 1]
    assert combined["baseline"] == "config=plain"


def test_run_index_like_label_keys_do_not_split_series(combine_module, build, tmp_path):
    reports = {
        f"r{i}.json": build.report(
            build.two_prompts(), labels={"config": "plain", "run": str(i)}, run_index=i
        )
        for i in range(2)
    }
    combined = combine_module.combine(runs_from(combine_module, reports, tmp_path))
    assert [item["name"] for item in combined["series"]] == ["config=plain"]
    assert combined["series"][0]["overall"]["run_count"] == 2


def test_without_labels_series_are_model_ids(combine_module, build, tmp_path):
    reports = {
        "x.json": build.report(build.two_prompts(), model_id="model-x"),
        "y.json": build.report(build.two_prompts(), model_id="model-y"),
    }
    combined = combine_module.combine(
        runs_from(combine_module, reports, tmp_path), baseline="model_id=model-y"
    )
    assert [item["name"] for item in combined["series"]] == ["model_id=model-y", "model_id=model-x"]


def test_pooled_and_mean_decode_rates_across_runs(
    combine_module, plain_and_draft_reports, tmp_path
):
    combined = combine_module.combine(
        runs_from(combine_module, plain_and_draft_reports, tmp_path), baseline="config=plain"
    )
    decode = by_name(combined)["config=plain"]["overall"]["decode_tokens_per_second"]
    # Per run: 100 + 10 decode tokens in 1.5 s; per request: 100 and 20 tokens/s.
    assert decode["pooled"] == pytest.approx(73.333, abs=1e-3)
    assert decode["mean"] == pytest.approx(60.0)
    assert decode["count"] == 4


def test_speedup_overall_and_per_cohort(combine_module, plain_and_draft_reports, tmp_path):
    combined = combine_module.combine(
        runs_from(combine_module, plain_and_draft_reports, tmp_path), baseline="config=plain"
    )
    comparison = by_name(combined)["config=draft"]["comparison"]
    assert comparison["decode_speedup_pooled"] == pytest.approx(2.0)
    assert comparison["decode_speedup_mean"] == pytest.approx(2.0)
    assert comparison["ttft_p50_ratio"] == pytest.approx(0.8)
    assert comparison["run_range_check"] == combine_module.RUN_RANGE_FASTER
    cohorts = comparison["cohorts"]["category"]
    assert set(cohorts) == {"coding", "writing"}
    assert cohorts["writing"]["decode_speedup_pooled"] == pytest.approx(2.0)


def test_cohort_summaries_and_pooled_acceptance(combine_module, plain_and_draft_reports, tmp_path):
    combined = combine_module.combine(
        runs_from(combine_module, plain_and_draft_reports, tmp_path), baseline="config=plain"
    )
    draft = by_name(combined)["config=draft"]
    coding = draft["cohorts"]["category"]["coding"]
    assert coding["request_count"] == 2
    assert coding["run_count"] == 2
    assert coding["decode_tokens_per_second"]["pooled"] == pytest.approx(200.0)
    assert draft["overall"]["draft"] == {
        "request_count": 4,
        "draft_tokens": 400.0,
        "draft_tokens_accepted": 300.0,
        "acceptance_rate": 0.75,
    }
    assert by_name(combined)["config=plain"]["overall"]["draft"] is None


def test_identical_outputs_within_series_and_against_baseline(
    combine_module, plain_and_draft_reports, tmp_path
):
    combined = combine_module.combine(
        runs_from(combine_module, plain_and_draft_reports, tmp_path), baseline="config=plain"
    )
    plain, draft = combined["series"]
    assert plain["identical_output"]["within_series"] == {
        "run_pairs": 1,
        "pairs": 2,
        "identical_pairs": 1,
        "rate": 0.5,
        "prompts": 2,
        "prompts_identical": 1,
    }
    assert plain["identical_output"]["vs_baseline"] is None
    assert draft["identical_output"]["within_series"]["rate"] == 1.0
    # Each draft run matches plain run 0 on both prompts and plain run 1 on prompt a only.
    assert draft["identical_output"]["vs_baseline"] == {
        "run_pairs": 4,
        "pairs": 8,
        "identical_pairs": 6,
        "rate": 0.75,
        "prompts": 2,
        "prompts_identical": 1,
    }


def test_single_run_has_no_within_series_rate(combine_module, build, tmp_path):
    reports = {"only.json": build.report(build.two_prompts(), labels={"config": "plain"})}
    combined = combine_module.combine(runs_from(combine_module, reports, tmp_path))
    assert combined["series"][0]["identical_output"]["within_series"] is None
    assert "single_run" in warning_codes(combined)


@pytest.mark.parametrize(
    ("series", "baseline", "expected"),
    [
        ([110.0, 120.0], [100.0, 105.0], "faster_in_every_run"),
        ([90.0, 95.0], [100.0, 105.0], "slower_in_every_run"),
        ([99.0, 110.0], [100.0, 105.0], "overlaps_baseline_runs"),
        ([150.0], [100.0, 105.0], "not_enough_runs"),
    ],
)
def test_run_range_check(combine_module, series, baseline, expected):
    assert combine_module.run_range_check(series, baseline) == expected


def test_mismatched_runs_are_warned(combine_module, build, tmp_path):
    reports = {
        "plain.json": build.report(build.two_prompts(), labels={"config": "plain"}),
        "other.json": build.report(
            build.two_prompts(),
            labels={"config": "other"},
            prompts_sha256="c" * 64,
            max_new_tokens=256,
            extra_body={"cache_prompt": False},
            gpu_driver="2.0.0",
        ),
    }
    combined = combine_module.combine(runs_from(combine_module, reports, tmp_path))
    assert {
        "different_prompts",
        "different_max_new_tokens",
        "different_extra_body",
        "different_hosts",
    } <= warning_codes(combined)


def test_matching_runs_have_no_mismatch_warnings(combine_module, plain_and_draft_reports, tmp_path):
    combined = combine_module.combine(runs_from(combine_module, plain_and_draft_reports, tmp_path))
    assert warning_codes(combined) == set()


def test_cached_prompt_tokens_are_warned(combine_module, build, tmp_path):
    requests = [build.request("a", cohorts={"category": "coding"}, cached_prompt_tokens=30)]
    reports = {
        f"r{i}.json": build.report(requests, labels={"config": "plain"}, run_index=i)
        for i in range(2)
    }
    combined = combine_module.combine(runs_from(combine_module, reports, tmp_path))
    assert "prompt_cache" in warning_codes(combined)


def test_cohort_values_sort_naturally(combine_module, build, tmp_path):
    requests = [
        build.request(str(i), cohorts={"bucket": bucket}, index=i)
        for i, bucket in enumerate(["qualitative", "16k", "2k", "1k"])
    ]
    reports = {
        "r.json": build.report(requests, labels={"config": "plain"}, cohort_columns=("bucket",))
    }
    combined = combine_module.combine(runs_from(combine_module, reports, tmp_path))
    assert list(combined["series"][0]["cohorts"]["bucket"]) == ["1k", "2k", "16k", "qualitative"]


@pytest.mark.parametrize("spec", ["config=missing", "server=llama.cpp"])
def test_baseline_must_match_exactly_one_series(
    combine_module, plain_and_draft_reports, tmp_path, spec
):
    runs = runs_from(combine_module, plain_and_draft_reports, tmp_path)
    with pytest.raises(combine_module.ReportError, match="matches"):
        combine_module.combine(runs, baseline=spec)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema_version": 3}, "schema_version"),
        ({"benchmark_info": {"runtime": "onnxruntime"}}, "runtime"),
        ({"requests": None}, "requests"),
    ],
)
def test_unusable_reports_are_rejected(combine_module, build, change, message):
    report = {**build.report(build.two_prompts()), **change}
    with pytest.raises(combine_module.ReportError, match=message):
        combine_module.check_report(report, "bad.json")


def test_cli_writes_combined_json_without_local_paths(
    combine_module, build, plain_and_draft_reports, tmp_path, capsys
):
    folder = tmp_path / "reports"
    build.write(folder, plain_and_draft_reports)
    output = tmp_path / "out" / "combined.json"
    assert combine_module.main([str(folder), "-o", str(output), "--baseline", "config=plain"]) == 0
    text = output.read_text(encoding="utf-8")
    combined = json.loads(text)
    assert combined["validation"] == "json-schema"
    assert [entry["file"] for entry in combined["inputs"]] == [
        "plain-0.json",
        "plain-1.json",
        "draft-0.json",
        "draft-1.json",
    ]
    assert str(tmp_path) not in text
    assert str(tmp_path).replace("\\", "\\\\") not in text
    assert "Baseline: config=plain" in capsys.readouterr().out


def test_cli_reports_bad_input(combine_module, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
    assert combine_module.main([str(bad), "-o", str(tmp_path / "out.json")]) == 2
    assert "schema_version 2" in capsys.readouterr().err
