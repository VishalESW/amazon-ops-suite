"""One-time: produce a shrunken clean template from the master.

Deletes example DATA rows (keeps control panels, headers, and the template
formula rows above each data table) so the runtime template loads/saves fast.
Source: _ngram_source.xlsx  ->  assets/campaign_template.xlsx
"""
import time, openpyxl

SRC = "_ngram_source.xlsx"
DST = "assets/campaign_template.xlsx"

# sheet -> first data row to delete from (everything from here down is removed)
DEL_FROM = {
    "Campaign Naming, Bids & Targets": 2,
    "PAT": 3,
    "Semantics": 4,
    "ASIN List": 3,
    "Master Keyword List": 3,
    "Product Opportunity Explorer": 23,
    "Helium10 Reverse ASIN Search": 16,
    "Search Term Report": 7,
    "Brand Analytics - ASIN Filter": 18,
    "Brand": 7,
    "SQP Report B0DQV3BZD7": 12,
}
BA_DEL_FROM = 10  # any "BA TST" tab

t0 = time.time()
wb = openpyxl.load_workbook(SRC)
print("loaded %.1fs" % (time.time() - t0))

for ws in wb.worksheets:
    name = ws.title
    start = DEL_FROM.get(name)
    if start is None and name.startswith("BA TST"):
        start = BA_DEL_FROM
    if start is None:
        continue
    n = ws.max_row - start + 1
    if n > 0:
        t = time.time()
        ws.delete_rows(start, n)
        print("  %-34s deleted %5d rows  %.1fs" % (name, n, time.time() - t))

t = time.time()
wb.save(DST)
print("saved %.1fs total %.1fs" % (time.time() - t, time.time() - t0))
