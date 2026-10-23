import streamlit as st
import pandas as pd
import re
import io
import zipfile

# ── Page Config ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="DATA WASHING MACHINE", layout="wide")

# ── Styling ──────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .metric-card {
        background: linear-gradient(135deg, #1e1e2f 0%, #2d2d44 100%);
        border: 1px solid #3a3a5c;
        border-radius: 12px;
        padding: 20px;
        text-align: center;
        margin-bottom: 10px;
    }
    .metric-card h3 { color: #a78bfa; margin: 0; font-size: 14px; }
    .metric-card h1 { color: #e2e8f0; margin: 5px 0 0 0; font-size: 32px; }
    .step-pass { color: #4ade80; font-weight: 600; }
    .step-fail { color: #f87171; font-weight: 600; }
</style>
""", unsafe_allow_html=True)

# ── Constants ────────────────────────────────────────────────────────────────
MOBILE_KEYWORDS = [
    "mobile", "phone", "telephone", "workphone", "cellphone", "cell",
    "contact", "tel", "mob", "whatsapp",
]

# Delimiters that may separate two numbers inside one cell
CELL_SPLIT_RE = re.compile(r"[/,;|\n\\]+")


def detect_mobile_columns(columns: list[str]) -> list[str]:
    """Auto-detect ALL columns whose name contains a mobile/phone keyword
    (substring / LIKE %keyword% match)."""
    matched = []
    for col in columns:
        low = col.lower().strip()
        for kw in MOBILE_KEYWORDS:
            if kw in low:                       # ← LIKE %keyword%
                matched.append(col)
                break
    return matched


def _float_safe_str(val) -> str:
    """Convert a value to string, stripping the '.0' that pandas adds
    when it reads integers from Excel as floats (e.g. 556691214.0)."""
    if pd.isna(val):
        return ""
    s = str(val).strip()
    if s.lower() == "nan":
        return ""
    # Remove trailing .0 from float representations (e.g. "556691214.0")
    if re.fullmatch(r"-?\d+\.0+", s):
        s = s.split(".")[0]
    return s


def _split_cell(value) -> list[str]:
    """Split a cell that may contain multiple numbers separated by / , ; etc.
    Returns a list of digit-only strings (empty strings filtered out)."""
    raw = _float_safe_str(value)
    parts = CELL_SPLIT_RE.split(raw)
    cleaned = [re.sub(r"[^\d]", "", p) for p in parts]
    return [c for c in cleaned if c]           # drop empties


def _clean_digits(series: pd.Series) -> pd.Series:
    """Strip everything except digits from a Series."""
    return series.apply(_float_safe_str).apply(
        lambda x: re.sub(r"[^\d]", "", x)
    )


# ─────────────────────────────────────────────────────────────────────────────
#  UNIFIED PIPELINE – handles 1 or more mobile columns, and multi-number cells
# ─────────────────────────────────────────────────────────────────────────────
def apply_rules(df: pd.DataFrame, mobile_cols: list[str]):
    stats = {}
    stats["total"] = len(df)
    MOBILE_COL = "Mobile Number"
    OUTPUT_COLS = ["NAME", MOBILE_COL, "EMAIL", "PROJECT", "MASTER PROJECT"]

    # Map available metadata columns to the fixed output schema.
    source_cols = {
        output_col: next(
            (col for col in df.columns
             if col not in mobile_cols
             and col.lower().strip() == output_col.lower()),
            None,
        )
        for output_col in OUTPUT_COLS
        if output_col != MOBILE_COL
    }

    # ── Rule 1: clean & split ────────────────────────────────────────────
    #   For each row, gather ALL numbers from all detected mobile columns.
    #   A single cell can contain 2 numbers separated by / , ; etc.
    rows = []
    for _, row in df.iterrows():
        base = {
            output_col: (row[source_col] if source_col is not None else "")
            for output_col, source_col in source_cols.items()
        }
        numbers = []
        for mc in mobile_cols:
            numbers.extend(_split_cell(row[mc]))

        # Deduplicate within the same row
        unique_numbers = list(dict.fromkeys(numbers))   # preserves order

        if not unique_numbers:
            continue  # skip row – no valid digits at all

        # same → one row; different → one row per distinct number
        for num in unique_numbers:
            r = base.copy()
            r[MOBILE_COL] = num
            rows.append(r)

    working = pd.DataFrame(rows, columns=OUTPUT_COLS)
    if working.empty:
        stats.update({"after_rule1": 0, "after_rule2": 0,
                      "after_rule3": 0, "after_rule4": 0})
        return working, stats

    stats["after_rule1"] = len(working)

    # ── Rule 2: remove duplicate rows on mobile and project values ───────
    dedupe_cols = [MOBILE_COL, "MASTER PROJECT", "PROJECT"]
    working = working.drop_duplicates(subset=dedupe_cols, keep="first")
    stats["after_rule2"] = len(working)

    # ── Rule 3: 9th digit from right == 5, keep only last 9 digits ──────
    working = working[
        working[MOBILE_COL].apply(lambda x: len(x) >= 9 and x[-9] == "5")
    ]
    working[MOBILE_COL] = working[MOBILE_COL].str[-9:]
    stats["after_rule3"] = len(working)

    # ── Rule 4: prepend +971 ────────────────────────────────────────────
    working[MOBILE_COL] = "+971" + working[MOBILE_COL]
    stats["after_rule4"] = len(working)

    return working, stats


# ── App ──────────────────────────────────────────────────────────────────────
st.title("DATA CLEANER")
st.caption("Upload a file → apply rules per sheet → download clean data")

uploaded = st.file_uploader(
    "Upload your data files",
    type=["csv", "xlsx", "xls"],
    accept_multiple_files=True,
    help="CSV or Excel files",
)

if uploaded:
    files = []

    for file_index, uploaded_file in enumerate(uploaded):
        file_base = uploaded_file.name.rsplit(".", 1)[0]
        is_csv = uploaded_file.name.lower().endswith(".csv")

        try:
            if is_csv:
                sheets = {"Sheet1": pd.read_csv(uploaded_file)}
            else:
                sheets = pd.read_excel(uploaded_file, sheet_name=None)
        except Exception as e:
            st.error(f"Failed to read {uploaded_file.name}: {e}")
            continue

        files.append((file_index, uploaded_file.name, file_base, sheets))

        sheet_names = list(sheets.keys())
        if len(sheet_names) > 1:
            st.success(
                f"**{uploaded_file.name}**: found **{len(sheet_names)}** sheets: "
                f"{', '.join(sheet_names)}"
            )
        else:
            st.success(
                f"**{uploaded_file.name}**: loaded **{len(sheets[sheet_names[0]]):,}** rows × "
                f"**{len(sheets[sheet_names[0]].columns)}** columns"
            )

    if files and st.button("🚀 Run Cleaning Rules", type="primary"):
        output_files = []
        archive_names = set()

        for file_index, file_name, file_base, sheets in files:
            sheet_names = list(sheets.keys())
            multi_sheet = len(sheet_names) > 1

            if len(files) > 1:
                st.divider()
                st.header(f"📁 File: {file_name}")

            for sheet_name in sheet_names:
                df = sheets[sheet_name]

                if multi_sheet:
                    st.divider()
                    st.header(f"📄 Sheet: {sheet_name}")
                st.write(f"**{len(df):,}** rows × **{len(df.columns)}** columns")

                # ── Detect mobile columns ────────────────────────────────
                detected = detect_mobile_columns(df.columns.tolist())

                if not detected:
                    st.warning(
                        f"⚠️ {file_name} / {sheet_name}: "
                        "No mobile column found. Skipping."
                    )
                    continue

                st.info(
                    f"Auto-detected **{len(detected)}** mobile column(s): "
                    f"**{', '.join(detected)}**"
                )

                # ── Apply rules ──────────────────────────────────────────
                cleaned_df, stats = apply_rules(df, detected)

                # ── Step-by-step metrics ─────────────────────────────────
                st.subheader("Step-by-step Results")

                labels = [
                    ("Total Rows", stats["total"]),
                    ("Rule 1 – Clean & Split", stats["after_rule1"]),
                    ("Rule 2 – Deduplicated", stats["after_rule2"]),
                    ("Rule 3 – 9th Digit = 5", stats["after_rule3"]),
                    ("Rule 4 – +971 Prefixed", stats["after_rule4"]),
                ]

                metric_cols = st.columns(len(labels))
                for c, (label, val) in zip(metric_cols, labels):
                    c.markdown(
                        f'<div class="metric-card"><h3>{label}</h3><h1>{val:,}</h1></div>',
                        unsafe_allow_html=True,
                    )

                # ── Preview ──────────────────────────────────────────────
                st.subheader("Cleaned Data Preview")
                st.dataframe(cleaned_df.head(50), use_container_width=True)

                # ── Add cleaned workbook to the ZIP archive ───────────────
                buffer = io.BytesIO()
                cleaned_df.to_excel(buffer, index=False, engine="openpyxl")

                if multi_sheet:
                    out_name = f"{file_base} {sheet_name} CLEANED.xlsx"
                else:
                    out_name = f"{file_base} CLEANED.xlsx"

                archive_name = out_name
                duplicate_number = 2
                while archive_name in archive_names:
                    name, extension = out_name.rsplit(".", 1)
                    archive_name = f"{name} ({duplicate_number}).{extension}"
                    duplicate_number += 1

                archive_names.add(archive_name)
                output_files.append((archive_name, buffer.getvalue()))

        if output_files:
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for archive_name, file_data in output_files:
                    archive.writestr(archive_name, file_data)
            zip_buffer.seek(0)

            st.success(f"Prepared **{len(output_files)}** cleaned file(s) in one ZIP archive.")
            st.download_button(
                label="⬇️ Download All Cleaned Files (ZIP)",
                data=zip_buffer,
                file_name="CLEANED_FILES.zip",
                mime="application/zip",
                type="primary",
                key="download_all_cleaned_files",
            )
        else:
            st.warning("No files were processed because no mobile column was found.")

