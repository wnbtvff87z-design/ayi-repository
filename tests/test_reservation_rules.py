import sys
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
sys.path.insert(0, str(WEB))
from reservation_rules import (
    MAX_LISTED_RESERVATIONS,
    explicit_choice,
    format_reservation_page,
    meal_filter,
    reservation_page,
    resolve_meal,
    sort_reservations,
)
sys.path.remove(str(WEB))


def slots(*times):
    return [{"date": "2030-05-01", "time": value} for value in times]


def test_lunch_and_dinner_use_fixed_inclusive_start_exclusive_end_windows():
    rows = slots("12:29", "12:30", "13:00", "15:29", "15:30",
                 "18:59", "19:00", "20:00", "22:59", "23:00")
    assert [row["time"] for row in meal_filter(rows, "lunch")] == [
        "12:30", "13:00", "15:29"
    ]
    assert [row["time"] for row in meal_filter(rows, "dinner")] == [
        "19:00", "20:00", "22:59"
    ]


def test_large_gaps_and_partial_availability_do_not_change_service_windows():
    rows = slots("12:30", "14:30", "15:29", "19:00", "20:00", "20:30", "22:30")
    assert [row["time"] for row in meal_filter(rows, "lunch")] == [
        "12:30", "14:30", "15:29"
    ]
    assert [row["time"] for row in meal_filter(rows, "dinner")] == [
        "19:00", "20:00", "20:30", "22:30"
    ]
    sparse = slots("20:00", "20:30", "22:30")
    assert meal_filter(sparse, "dinner") == sparse


def test_explicit_meal_then_confirmed_state_outrank_model_proposal():
    assert resolve_meal("cena", None, "lunch") == "dinner"
    assert resolve_meal("comida", None, "dinner") == "lunch"
    assert resolve_meal("consulta", "dinner", "lunch") == "dinner"
    assert resolve_meal("consulta", None, "lunch") == "lunch"


def test_reservation_pages_cover_small_and_large_lists_without_skipping_items():
    for count in (1, 5, 6, 20, 21, 31):
        rows = [{"code": str(i), "date": f"2030-05-{i:02d}", "time": "20:00"}
                for i in range(1, count + 1)]
        first, next_offset = reservation_page(rows)
        assert len(first) == min(count, MAX_LISTED_RESERVATIONS)
        assert next_offset == (MAX_LISTED_RESERVATIONS if count > MAX_LISTED_RESERVATIONS else None)
        if next_offset is not None:
            second, final_offset = reservation_page(rows, next_offset)
            assert [row["code"] for row in first + second] == [row["code"] for row in rows]
            assert final_offset is None


def test_reservation_display_and_numeric_choice_use_the_same_page_indices():
    rows = [{"code": str(i), "date": "2030-05-01", "time": f"{i // 60:02d}:{i % 60:02d}"}
            for i in range(1, 22)]
    first, next_offset, message = format_reservation_page(rows, 0, "cancelar", lambda d, t: f"{d} {t}")
    assert len(first) == 20 and "20. 2030-05-01" in message
    assert "21. 2030-05-01" not in message and "Hay más reservas" in message
    assert explicit_choice("20", len(first)) == 20
    assert explicit_choice("veinte", len(first)) == 20
    assert explicit_choice("21", len(first)) is None
    second, final_offset, _ = format_reservation_page(rows, next_offset, "cancelar", lambda d, t: f"{d} {t}")
    assert len(second) == 1 and second[0]["code"] == "21"
    assert final_offset is None and explicit_choice("1", len(second)) == 1


def test_reservations_sort_latest_scheduled_date_and_time_first():
    rows = [
        {"code": "early", "slot_date": "2030-05-01", "start_time": "21:00"},
        {"code": "late", "slot_date": "2030-05-03", "start_time": "20:00"},
        {"code": "same-day-late", "slot_date": "2030-05-02", "start_time": "22:00"},
        {"code": "same-day-early", "slot_date": "2030-05-02", "start_time": "19:00"},
    ]
    assert [row["code"] for row in sort_reservations(rows)] == [
        "late", "same-day-late", "same-day-early", "early"
    ]
