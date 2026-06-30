import tempfile
import threading
import uuid
from io import BytesIO
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from voter_classifier import SetupError, process_pdf, validate_setup


BASE_DIR = Path(__file__).resolve().parent
ALLOWED_EXTENSIONS = {".pdf"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024
DOWNLOADS = {}
JOBS = {}
JOBS_LOCK = threading.Lock()


def allowed_file(filename):
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def parse_int(value):
    value = (value or "").strip()
    return int(value) if value else None


def update_job(job_id, **values):
    with JOBS_LOCK:
        job = JOBS.setdefault(job_id, {})
        job.update(values)


def get_job(job_id):
    with JOBS_LOCK:
        return JOBS.get(job_id, {}).copy()


def run_pdf_job(job_id, file_bytes, filename, first_page, last_page, dpi, lang):
    def on_progress(done_pages, total_pages, page_number, message=None):
        percent = 10
        if total_pages:
            percent = 10 + int((done_pages / total_pages) * 80)
        update_job(
            job_id,
            percent=min(percent, 90),
            message=message or f"Processed page {page_number} ({done_pages} of {total_pages})",
        )

    try:
        update_job(job_id, status="processing", percent=5, message="Saving uploaded PDF...")
        with tempfile.TemporaryDirectory(prefix="voter_pdf_") as temp_dir:
            temp_dir_path = Path(temp_dir)
            pdf_path = temp_dir_path / filename
            output_dir = temp_dir_path / "reports"
            pdf_path.write_bytes(file_bytes)

            update_job(job_id, percent=10, message="Converting PDF pages for OCR...")
            result = process_pdf(
                pdf_path,
                output_dir=output_dir,
                first_page=first_page,
                last_page=last_page,
                dpi=dpi,
                lang=lang,
                progress_callback=on_progress,
            )

            update_job(job_id, percent=95, message="Preparing Excel reports...")
            DOWNLOADS[job_id] = {
                result["details_path"].name: result["details_path"].read_bytes(),
                result["summary_path"].name: result["summary_path"].read_bytes(),
            }
            result["job_id"] = job_id
            rows = result["data"].head(250).to_dict("records")
            update_job(
                job_id,
                status="complete",
                percent=100,
                message="Processing complete.",
                result=result,
                rows=rows,
            )
    except Exception as exc:
        update_job(job_id, status="error", percent=100, message=str(exc), error=str(exc))


@app.route("/", methods=["GET", "POST"])
def index():
    setup_status = None
    error = None
    result = None
    rows = []
    job_id = request.args.get("job_id")

    try:
        setup_status = validate_setup()
    except SetupError as exc:
        error = str(exc)

    if job_id:
        job = get_job(job_id)
        if job.get("status") == "complete":
            result = job.get("result")
            rows = job.get("rows", [])
        elif job.get("status") == "error":
            error = job.get("error") or "Processing failed."

    if request.method == "POST" and not error:
        uploaded_file = request.files.get("pdf")
        if not uploaded_file or not uploaded_file.filename:
            error = "Choose a PDF file first."
        elif not allowed_file(uploaded_file.filename):
            error = "Only PDF files are supported."
        else:
            job_id = uuid.uuid4().hex
            filename = secure_filename(uploaded_file.filename)

            try:
                with tempfile.TemporaryDirectory(prefix="voter_pdf_") as temp_dir:
                    temp_dir_path = Path(temp_dir)
                    pdf_path = temp_dir_path / filename
                    output_dir = temp_dir_path / "reports"
                    uploaded_file.save(pdf_path)

                    result = process_pdf(
                        pdf_path,
                        output_dir=output_dir,
                        first_page=parse_int(request.form.get("first_page")),
                        last_page=parse_int(request.form.get("last_page")),
                        dpi=parse_int(request.form.get("dpi")) or 300,
                        lang=(request.form.get("lang") or "hin+eng").strip(),
                    )

                    DOWNLOADS[job_id] = {
                        result["details_path"].name: result["details_path"].read_bytes(),
                        result["summary_path"].name: result["summary_path"].read_bytes(),
                    }
                    result["job_id"] = job_id
                rows = result["data"].head(250).to_dict("records")
            except Exception as exc:
                error = str(exc)

    return render_template(
        "index.html",
        setup_status=setup_status,
        error=error,
        result=result,
        rows=rows,
    )


@app.route("/process", methods=["POST"])
def start_process():
    try:
        validate_setup()
    except SetupError as exc:
        return jsonify({"error": str(exc)}), 400

    uploaded_file = request.files.get("pdf")
    if not uploaded_file or not uploaded_file.filename:
        return jsonify({"error": "Choose a PDF file first."}), 400
    if not allowed_file(uploaded_file.filename):
        return jsonify({"error": "Only PDF files are supported."}), 400

    try:
        first_page = parse_int(request.form.get("first_page"))
        last_page = parse_int(request.form.get("last_page"))
        dpi = parse_int(request.form.get("dpi")) or 300
    except ValueError:
        return jsonify({"error": "Page and DPI values must be valid numbers."}), 400

    job_id = uuid.uuid4().hex
    filename = secure_filename(uploaded_file.filename)
    file_bytes = uploaded_file.read()
    lang = (request.form.get("lang") or "hin+eng").strip()
    update_job(job_id, status="queued", percent=0, message="Queued for processing...")

    worker = threading.Thread(
        target=run_pdf_job,
        args=(job_id, file_bytes, filename, first_page, last_page, dpi, lang),
        daemon=True,
    )
    worker.start()
    return jsonify({"job_id": job_id})


@app.route("/progress/<job_id>")
def progress(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(
        {
            "status": job.get("status", "queued"),
            "percent": job.get("percent", 0),
            "message": job.get("message", ""),
            "error": job.get("error"),
        }
    )


@app.route("/download/<job_id>/<filename>")
def download(job_id, filename):
    safe_filename = secure_filename(filename)
    file_bytes = DOWNLOADS.get(job_id, {}).get(safe_filename)
    if file_bytes is None:
        return "File not found", 404
    return send_file(
        BytesIO(file_bytes),
        as_attachment=True,
        download_name=safe_filename,
    )


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, threaded=True, host="127.0.0.1", port=5000)
