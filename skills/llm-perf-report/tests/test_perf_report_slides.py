# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------

import json

import pytest


@pytest.fixture
def make_combined(combine_module, build, tmp_path):
    def make(reports, baseline=None):
        folder = tmp_path / "reports"
        runs = [combine_module.load_run(path) for path in build.write(folder, reports)]
        return combine_module.combine(runs, baseline=baseline)

    return make


def titles(slides):
    return [slide["title"] for slide in slides]


def charts(slides):
    return [slide for slide in slides if slide["layout"] == "chart"]


def test_deck_outline_with_draft_and_cohorts(slides_module, make_combined, plain_and_draft_reports):
    slides = slides_module.build_slides(make_combined(plain_and_draft_reports, "config=plain"))
    assert titles(slides) == [
        "Text generation speed report",
        "Summary",
        "Decode tokens per second by series",
        "Decode tokens per second by run",
        "Decode speedup vs config=plain",
        "Decode speedup vs config=plain by category",
        "Decode tokens per second by category",
        "Draft acceptance rate by category",
        "Speculative decoding",
        "Time to first token by series",
        "Time to first token (p50) by category",
        "Identical outputs",
        "Method and provenance",
        "Caveats",
    ]


def test_single_series_without_draft_has_no_comparison_slides(slides_module, make_combined, build):
    reports = {
        f"r{i}.json": build.report(build.two_prompts(), labels={"config": "plain"}, run_index=i)
        for i in range(2)
    }
    names = titles(slides_module.build_slides(make_combined(reports)))
    assert not any("speedup" in name or "Draft" in name or "Speculative" in name for name in names)
    assert "Identical outputs" in names


def test_summary_rows_state_speedup_and_run_range(
    slides_module, make_combined, plain_and_draft_reports
):
    summary = slides_module.build_slides(make_combined(plain_and_draft_reports, "config=plain"))[1]
    rows = {row[0]: dict(zip(summary["columns"], row, strict=True)) for row in summary["rows"]}
    draft = rows["config=draft"]
    assert draft["Speedup vs baseline"] == "2.00x"
    assert draft["Run-range check"] == "Faster in every run"
    assert draft["Draft acceptance"] == "75.0%"
    assert draft["Identical output vs baseline"] == "75.0% (6 of 8)"
    assert (
        rows["config=plain (baseline)"]["Identical output vs baseline"]
        == "Within baseline: 50.0% (1 of 2)"
    )


def test_speedup_by_cohort_chart(slides_module, make_combined, plain_and_draft_reports):
    slides = slides_module.build_slides(make_combined(plain_and_draft_reports, "config=plain"))
    chart = next(s for s in slides if s["title"] == "Decode speedup vs config=plain by category")[
        "chart"
    ]
    assert chart["categories"] == ["coding", "writing"]
    assert chart["series"] == [{"name": "config=draft", "values": [2.0, 2.0]}]
    assert chart["origin"] == 1.0
    assert chart["number_format"] == slides_module.RATIO


def test_every_chart_is_labeled(slides_module, make_combined, plain_and_draft_reports):
    for slide in charts(
        slides_module.build_slides(make_combined(plain_and_draft_reports, "config=plain"))
    ):
        chart = slide["chart"]
        assert chart["value_axis_title"]
        assert chart["series"]
        for series in chart["series"]:
            assert series["name"]
            assert len(series["values"]) == len(chart["categories"])


def test_tables_continue_on_more_slides(slides_module, make_combined, plain_and_draft_reports):
    slides = slides_module.build_slides(
        make_combined(plain_and_draft_reports, "config=plain"), max_table_rows=1
    )
    assert "Summary (1 of 2)" in titles(slides)
    assert "Summary (2 of 2)" in titles(slides)


def test_caveats_report_baseline_nondeterminism(
    slides_module, make_combined, plain_and_draft_reports
):
    caveats = slides_module.build_slides(make_combined(plain_and_draft_reports, "config=plain"))[-1]
    text = " ".join(caveats["bullets"])
    assert "not fully reproducible: 50.0% (1 of 2)" in text
    assert "below 1.00x is a valid result" in text


def test_provenance_lists_files_and_prompts(slides_module, make_combined, plain_and_draft_reports):
    slides = slides_module.build_slides(make_combined(plain_and_draft_reports, "config=plain"))
    rows = dict(next(s for s in slides if s["title"] == "Method and provenance")["rows"])
    assert rows["File (model)"] == "model.gguf, 1.0 KiB, sha256 bbbbbbbbbbbb"
    assert "prompts sha256 aaaaaaaaaaaa" in rows["Prompts"]


@pytest.mark.parametrize(
    ("values", "origin"),
    [([0.43, 0.6], 1.0), ([1.2, 2.5], 1.0), ([0.98, 1.01], 1.0)],
)
def test_origin_axis_bounds_contain_origin_and_values(slides_module, values, origin):
    low, high, unit = slides_module.origin_axis_bounds(values, origin)
    assert low <= min([*values, origin]) and high >= max([*values, origin])
    assert (high - low) / unit <= 12


def test_unsupported_combined_version_is_rejected(slides_module):
    with pytest.raises(ValueError, match="combined_version"):
        slides_module.build_slides({"combined_version": 99})


def test_pptx_has_native_charts_and_tables(
    slides_module, make_combined, plain_and_draft_reports, tmp_path
):
    from pptx import Presentation
    from pptx.util import Inches

    combined_path = tmp_path / "combined.json"
    combined_path.write_text(
        json.dumps(make_combined(plain_and_draft_reports, "config=plain")), encoding="utf-8"
    )
    deck = tmp_path / "deck.pptx"
    assert slides_module.main([str(combined_path), "-o", str(deck)]) == 0

    expected = slides_module.build_slides(json.loads(combined_path.read_text(encoding="utf-8")))
    presentation = Presentation(str(deck))
    assert presentation.slide_width == Inches(slides_module.SLIDE_WIDTH_IN)
    assert presentation.slide_height == Inches(slides_module.SLIDE_HEIGHT_IN)
    assert len(presentation.slides) == len(expected)
    for slide, spec in zip(presentation.slides, expected, strict=True):
        assert slide.shapes.title.text == spec["title"]
        chart_shapes = [shape for shape in slide.shapes if shape.has_chart]
        table_shapes = [shape for shape in slide.shapes if shape.has_table]
        assert len(chart_shapes) == (spec["layout"] == "chart")
        assert len(table_shapes) == (spec["layout"] == "table")
        if chart_shapes:
            chart = chart_shapes[0].chart
            assert chart.plots[0].has_data_labels
            assert [series.name for series in chart.plots[0].series] == [
                s["name"] for s in spec["chart"]["series"]
            ]
