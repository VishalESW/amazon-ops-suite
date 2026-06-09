"""N-Gram Analyzer blueprint.

This is the original single-purpose tool, unchanged in behaviour: upload a PPC
search-term CSV/Excel, run the N-gram pipeline, return result JSON + downloadable
Excel reports, plus the archive endpoints. Only the routing moved into a blueprint.
"""

import os
import uuid
from datetime import datetime

from flask import Blueprint, render_template, request, jsonify, current_app
from werkzeug.utils import secure_filename

from utils.csv_parser import (
    parse_csv, filter_asins, group_by_campaign, get_data_summary,
    validate_csv, filter_active_campaigns,
)
from utils.ngram_generator import generate_ngrams, get_ngram_summary
from utils.metrics import aggregate_ngram_metrics, get_campaign_summary
from utils.excel_writer import (
    create_excel_output, generate_output_filename, create_asin_report,
)
from utils.jsonutil import convert_numpy
from utils.archive_store import load_archive, save_archive

bp = Blueprint("ngram", __name__)


def allowed_file(filename: str) -> bool:
    return "." in filename and \
        filename.rsplit(".", 1)[1].lower() in current_app.config["ALLOWED_EXTENSIONS"]


def process_csv_file(filepath: str) -> dict:
    """Run a CSV/Excel file through the full N-gram analysis pipeline."""
    df = parse_csv(filepath)

    is_valid, missing_cols = validate_csv(df)
    if not is_valid:
        return {
            "success": False,
            "error": f"Missing required columns: {', '.join(missing_cols)}",
            "missing_columns": missing_cols,
        }

    initial_summary = get_data_summary(df)
    df = filter_active_campaigns(df)
    df_filtered, df_asins, asin_count = filter_asins(df)
    campaigns = group_by_campaign(df_filtered)

    processed_data = {}
    campaign_details = {}
    total_orders = 0

    for campaign_name, campaign_df in campaigns.items():
        ngrams = generate_ngrams(campaign_df)

        for ngram_type in ["monograms", "bigrams", "trigrams"]:
            if ngram_type in ngrams and not ngrams[ngram_type].empty:
                ngrams[ngram_type] = aggregate_ngram_metrics(ngrams[ngram_type])

        if "search_terms" in ngrams and not ngrams["search_terms"].empty:
            ngrams["search_terms"] = aggregate_ngram_metrics(ngrams["search_terms"])

        ngram_summary = get_ngram_summary(ngrams)
        campaign_metrics = get_campaign_summary(campaign_df)

        processed_data[campaign_name] = {
            "ngrams": ngrams,
            "summary": {**ngram_summary, **campaign_metrics},
        }
        campaign_details[campaign_name] = {
            "monograms": ngram_summary.get("monogram_count", 0),
            "bigrams": ngram_summary.get("bigram_count", 0),
            "trigrams": ngram_summary.get("trigram_count", 0),
            "search_terms": ngram_summary.get("search_term_count", 0),
            "spend": campaign_metrics.get("total_spend", 0),
            "sales": campaign_metrics.get("total_sales", 0),
            "orders": campaign_metrics.get("total_orders", 0),
            "impressions": campaign_metrics.get("total_impressions", 0),
            "clicks": campaign_metrics.get("total_clicks", 0),
        }
        total_orders += campaign_metrics.get("total_orders", 0)

    output_filename = generate_output_filename()
    output_path = os.path.join(current_app.config["OUTPUT_FOLDER"], output_filename)
    create_excel_output(processed_data, output_path)

    asin_filename = None
    if asin_count > 0 and not df_asins.empty:
        asin_filename = generate_output_filename(prefix="ASIN_Report")
        asin_output_path = os.path.join(current_app.config["OUTPUT_FOLDER"], asin_filename)
        create_asin_report(df_asins, asin_output_path)

    result = {
        "success": True,
        "output_file": output_filename,
        "asin_file": asin_filename,
        "summary": {
            "original_rows": initial_summary["total_rows"],
            "asins_removed": asin_count,
            "campaigns_processed": len(campaigns),
            "total_search_terms": initial_summary["total_search_terms"],
            "total_spend": initial_summary["total_spend"],
            "total_sales": initial_summary["total_sales"],
            "total_orders": total_orders,
        },
        "campaigns": list(campaigns.keys()),
        "campaign_details": campaign_details,
    }
    return convert_numpy(result)


@bp.route("/ngram")
def page():
    return render_template("ngram.html")


@bp.route("/upload", methods=["POST"])
def upload_file():
    try:
        if "file" not in request.files:
            return jsonify({"success": False, "error": "No file uploaded"}), 400
        file = request.files["file"]
        if file.filename == "":
            return jsonify({"success": False, "error": "No file selected"}), 400
        if not allowed_file(file.filename):
            return jsonify({"success": False, "error": "Invalid file type. Please upload a CSV or Excel file (.csv, .xlsx, .xls)."}), 400

        filename = secure_filename(file.filename)
        unique_filename = f"{uuid.uuid4().hex}_{filename}"
        filepath = os.path.join(current_app.config["UPLOAD_FOLDER"], unique_filename)
        file.save(filepath)

        result = process_csv_file(filepath)

        try:
            os.remove(filepath)
        except OSError:
            pass

        if result["success"]:
            return jsonify(convert_numpy(result))
        return jsonify(convert_numpy(result)), 400
    except Exception as e:  # noqa: BLE001 — surface processing errors to the client
        return jsonify({"success": False, "error": f"Processing error: {str(e)}"}), 500


@bp.route("/archive", methods=["GET", "POST"])
def archive():
    if request.method == "GET":
        return jsonify({"success": True, "archives": load_archive()})

    try:
        data = request.get_json()
        if not data or "filename" not in data:
            return jsonify({"success": False, "error": "No filename provided"}), 400

        archive_entry = {
            "id": str(uuid.uuid4()),
            "filename": data.get("filename"),
            "originalFilename": data.get("originalFilename", "Unknown"),
            "summary": data.get("summary", {}),
            "processedAt": data.get("processedAt", datetime.now().isoformat()),
        }
        archives = load_archive()
        archives.insert(0, archive_entry)
        save_archive(archives)
        return jsonify({"success": True, "archive": archive_entry})
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"Failed to archive: {str(e)}"}), 500


@bp.route("/archive/<archive_id>", methods=["GET", "DELETE"])
def archive_item(archive_id):
    archives = load_archive()
    archive_entry = None
    archive_index = None
    for i, item in enumerate(archives):
        if item.get("id") == archive_id:
            archive_entry = item
            archive_index = i
            break

    if archive_entry is None:
        return jsonify({"success": False, "error": "Archive not found"}), 404

    if request.method == "GET":
        return jsonify({"success": True, "archive": archive_entry})

    try:
        archives.pop(archive_index)
        save_archive(archives)
        return jsonify({"success": True, "message": "Archive deleted successfully"})
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"Failed to delete: {str(e)}"}), 500
