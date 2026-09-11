from chainledger.backfill.core import compute_gaps


def test_no_coverage_everything_is_gap():
    assert compute_gaps(0, 30, []) == [(0, 30)]


def test_fully_covered_no_gaps():
    assert compute_gaps(0, 30, [(0, 30)]) == []


def test_gap_between_ranges():
    assert compute_gaps(0, 30, [(0, 9), (21, 30)]) == [(10, 20)]


def test_adjacent_ranges_merge():
    assert compute_gaps(0, 15, [(0, 5), (6, 10)]) == [(11, 15)]


def test_overlapping_ranges_merge():
    assert compute_gaps(0, 30, [(1, 5), (3, 8)]) == [(0, 0), (9, 30)]


def test_edges_partially_covered():
    assert compute_gaps(10, 20, [(5, 15)]) == [(16, 20)]
    assert compute_gaps(10, 20, [(15, 25)]) == [(10, 14)]


def test_covered_beyond_range():
    assert compute_gaps(0, 30, [(0, 50)]) == []


def test_empty_range():
    assert compute_gaps(30, 29, []) == []
