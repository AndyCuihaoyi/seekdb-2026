#!/usr/bin/env python3
"""Account for the measured query time with the measured cost model.

The model is fitted by tools/decompose_query.py, which reports the cost of
one query in microseconds against the number of rows in the table:

    per_query_us = fixed_us + per_row_ns * rows / 1000

The bench runs 1000 queries, half of them filtered.
"""
FIXED_US = 333.0                     # mean of the two intercepts
PER_ROW_NS_UNFILTERED = 44.554      # ns per scanned row, no filter
PER_ROW_NS_FILTERED = 118.007       # ns per scanned row, with a filter
ROWS = 10000
NQ = 1000
NQ_FILTERED = 500

# One query, in microseconds.
unfiltered_us = FIXED_US + PER_ROW_NS_UNFILTERED * ROWS / 1000.0
filtered_us = FIXED_US + PER_ROW_NS_FILTERED * ROWS / 1000.0

fixed_total = NQ * FIXED_US / 1000.0
unfiltered_rows = NQ_FILTERED * PER_ROW_NS_UNFILTERED * ROWS / 1000.0 / 1000.0
filtered_rows = NQ_FILTERED * PER_ROW_NS_FILTERED * ROWS / 1000.0 / 1000.0
predicate = NQ_FILTERED * (PER_ROW_NS_FILTERED - PER_ROW_NS_UNFILTERED) * ROWS / 1000.0 / 1000.0
total = fixed_total + unfiltered_rows + filtered_rows

print(f"one unfiltered query: {unfiltered_us:8.1f} us")
print(f"one filtered query  : {filtered_us:8.1f} us")
print()
print(f"fixed cost per query      : {fixed_total:8.1f} ms  ({100*fixed_total/total:5.1f}%)")
print(f"per-row cost, unfiltered  : {unfiltered_rows:8.1f} ms  ({100*unfiltered_rows/total:5.1f}%)")
print(f"per-row cost, filtered    : {filtered_rows:8.1f} ms  ({100*filtered_rows/total:5.1f}%)")
print(f"  of which the predicate  : {predicate:8.1f} ms  ({100*predicate/total:5.1f}%)")
print()
print(f"modelled total             : {total:8.1f} ms")
print(f"measured total (bench.py)  : {1360.1:8.1f} ms")
print(f"difference                 : {1360.1-total:8.1f} ms "
      f"(the model fits the warm cache; the bench starts cold)")
