"""e-Stat rows become age bands and household sizes only when they form one clear axis.

The total row of an axis (総数) is left out; any other row that cannot be read, or
bands that repeat or overlap, leave the statistics unused, and the notes tell the
operator which happened. The Project screen's preview over these statistics is checked
in tests/unit/web/test_workspace.py.
"""

from __future__ import annotations

import pytest

from sheltersim.region.models import StatisticalObservation
from sheltersim.region.population import (
    age_bands_from_observations,
    household_mix_from_observations,
    population_statistics_notes,
)


def _rows(labels: list[tuple[str, float]], unit: str = "人", **other: str):
    return [
        StatisticalObservation(
            table_id="0000000001",
            title="synthetic table",
            area_code="99901",
            category_codes={"cat01": str(index), **{key: "1" for key in other}},
            category_labels={"cat01": label, **other},
            value=value,
            unit=unit,
            source_id="SRC-ESTAT-API",
            content_sha256="0" * 64,
        )
        for index, (label, value) in enumerate(labels)
    ]


AGES = [("0～14歳", 10), ("15～64歳", 60), ("65歳以上", 30)]


@pytest.mark.parametrize("total", ["総数", "総計", "合計", "計", "総数 "])
def test_the_total_row_of_the_age_axis_is_left_out(total):
    rows = _rows([(total, 100), *AGES])

    bands = age_bands_from_observations(rows)

    assert [(band.label, band.population_count) for band in bands] == AGES
    assert population_statistics_notes(rows) == (
        f"統計の合計の行（{total}）を除いて年齢構成に使いました。",
    )


def test_a_total_in_another_selected_dimension_does_not_hide_the_age_axis():
    # The operator selected 総数 of the sex dimension; every row carries it.
    rows = _rows(AGES, cat02="総数")

    assert [band.label for band in age_bands_from_observations(rows)] == [
        label for label, _ in AGES
    ]


@pytest.mark.parametrize(
    "rows",
    [
        _rows([("0～4歳", 5), ("5～9歳", 5), ("10～14歳", 5), ("15歳未満", 15), ("15歳以上", 85)]),
        _rows([*AGES, ("不詳", 3)]),
        _rows(AGES, cat02="男") + _rows(AGES, cat02="女"),
    ],
    ids=["subtotal overlaps its bands", "unknown age", "unselected sex"],
)
def test_statistics_that_are_not_one_age_axis_are_unused_and_reported(rows):
    assert age_bands_from_observations(rows) == ()
    (note,) = population_statistics_notes(rows)
    assert note.startswith(f"取得した統計（{len(rows)}行）は、年齢構成にも世帯人数にも使えません")


def test_the_total_row_of_household_sizes_is_left_out():
    rows = _rows([("総数", 100), ("1人世帯", 40), ("2人世帯", 35), ("3人世帯", 25)], unit="世帯")

    mix = household_mix_from_observations(rows)

    assert mix.size_weights == {1: 40, 2: 35, 3: 25}
    assert population_statistics_notes(rows) == (
        "統計の合計の行（総数）を除いて世帯人数に使いました。",
    )


def test_statistics_used_as_they_are_need_no_note():
    assert population_statistics_notes(_rows(AGES)) == ()
    assert population_statistics_notes([]) == ()
