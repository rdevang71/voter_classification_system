import tempfile
import threading
import time
import uuid
from io import BytesIO
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from voter_classifier import DEFAULT_DPI, DEFAULT_OCR_LANG, SetupError, process_pdfs, validate_setup


BASE_DIR = Path(__file__).resolve().parent
ALLOWED_EXTENSIONS = {".pdf"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
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


def format_duration(seconds):
    if seconds is None:
        return "Estimating..."
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} sec"

    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {remaining_seconds} sec" if remaining_seconds else f"{minutes} min"

    hours, minutes = divmod(minutes, 60)
    parts = [f"{hours} hr"]
    if minutes:
        parts.append(f"{minutes} min")
    if remaining_seconds:
        parts.append(f"{remaining_seconds} sec")
    return " ".join(parts)


def estimate_eta(job):
    total_pages = int(job.get("total_pages") or 0)
    done_pages = int(job.get("done_pages") or 0)
    started_at = job.get("started_at")
    if not total_pages or not done_pages or not started_at:
        return None

    remaining_pages = max(0, total_pages - done_pages)
    if not remaining_pages:
        return 0

    elapsed_seconds = max(1, time.time() - started_at)
    seconds_per_page = elapsed_seconds / done_pages
    return remaining_pages * seconds_per_page


def result_to_downloads(result):
    return {
        result["details_path"].name: result["details_path"].read_bytes(),
        result["summary_path"].name: result["summary_path"].read_bytes(),
    }


def run_pdf_job(job_id, uploaded_files, first_page, last_page, dpi, lang):
    def on_progress(done_pages, total_pages, page_number, message=None):
        percent = 10
        if total_pages:
            percent = 10 + int((done_pages / total_pages) * 80)
        update_job(
            job_id,
            percent=min(percent, 90),
            done_pages=done_pages,
            total_pages=total_pages,
            message=message or f"Processed page {page_number} ({done_pages} of {total_pages})",
        )

    try:
        update_job(
            job_id,
            status="processing",
            percent=5,
            started_at=time.time(),
            done_pages=0,
            total_pages=0,
            message="Saving uploaded PDFs...",
        )
        with tempfile.TemporaryDirectory(prefix="voter_pdf_") as temp_dir:
            temp_dir_path = Path(temp_dir)
            pdf_paths = []
            output_dir = temp_dir_path / "reports"
            for index, uploaded_file in enumerate(uploaded_files, start=1):
                pdf_path = temp_dir_path / f"{index:03d}_{uploaded_file['filename']}"
                pdf_path.write_bytes(uploaded_file["content"])
                pdf_paths.append(pdf_path)

            update_job(job_id, percent=10, message="Converting PDF pages for OCR...")
            result = process_pdfs(
                pdf_paths,
                output_dir=output_dir,
                first_page=first_page,
                last_page=last_page,
                dpi=dpi,
                lang=lang,
                progress_callback=on_progress,
            )

            update_job(job_id, percent=95, message="Preparing Excel reports...")
            DOWNLOADS[job_id] = result_to_downloads(result)
            result["job_id"] = job_id
            rows = result["data"].to_dict("records")
            update_job(
                job_id,
                status="complete",
                percent=100,
                done_pages=get_job(job_id).get("total_pages", 0),
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
        uploaded_files = [file for file in request.files.getlist("pdf") if file and file.filename]
        invalid_files = [file.filename for file in uploaded_files if not allowed_file(file.filename)]
        if not uploaded_files:
            error = "Choose at least one PDF file first."
        elif invalid_files:
            error = "Only PDF files are supported."
        else:
            job_id = uuid.uuid4().hex

            try:
                with tempfile.TemporaryDirectory(prefix="voter_pdf_") as temp_dir:
                    temp_dir_path = Path(temp_dir)
                    output_dir = temp_dir_path / "reports"
                    pdf_paths = []
                    for index, uploaded_file in enumerate(uploaded_files, start=1):
                        filename = secure_filename(uploaded_file.filename)
                        pdf_path = temp_dir_path / f"{index:03d}_{filename}"
                        uploaded_file.save(pdf_path)
                        pdf_paths.append(pdf_path)

                    result = process_pdfs(
                        pdf_paths,
                        output_dir=output_dir,
                        first_page=parse_int(request.form.get("first_page")),
                        last_page=parse_int(request.form.get("last_page")),
                        dpi=parse_int(request.form.get("dpi")) or DEFAULT_DPI,
                        lang=(request.form.get("lang") or DEFAULT_OCR_LANG).strip(),
                    )

                    DOWNLOADS[job_id] = result_to_downloads(result)
                    result["job_id"] = job_id
                rows = result["data"].to_dict("records")
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

    uploaded_files = [file for file in request.files.getlist("pdf") if file and file.filename]
    if not uploaded_files:
        return jsonify({"error": "Choose at least one PDF file first."}), 400
    if any(not allowed_file(file.filename) for file in uploaded_files):
        return jsonify({"error": "Only PDF files are supported."}), 400

    try:
        first_page = parse_int(request.form.get("first_page"))
        last_page = parse_int(request.form.get("last_page"))
        dpi = parse_int(request.form.get("dpi")) or DEFAULT_DPI
    except ValueError:
        return jsonify({"error": "Page and DPI values must be valid numbers."}), 400

    job_id = uuid.uuid4().hex
    files_payload = [
        {"filename": secure_filename(file.filename), "content": file.read()}
        for file in uploaded_files
    ]
    lang = (request.form.get("lang") or DEFAULT_OCR_LANG).strip()
    update_job(
        job_id,
        status="queued",
        percent=0,
        done_pages=0,
        total_pages=0,
        message="Queued for processing...",
    )

    worker = threading.Thread(
        target=run_pdf_job,
        args=(job_id, files_payload, first_page, last_page, dpi, lang),
        daemon=True,
    )
    worker.start()
    return jsonify({"job_id": job_id})


@app.route("/progress/<job_id>")
def progress(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    elapsed_seconds = None
    started_at = job.get("started_at")
    if started_at:
        elapsed_seconds = max(0, time.time() - started_at)
    eta_seconds = estimate_eta(job)
    return jsonify(
        {
            "status": job.get("status", "queued"),
            "percent": job.get("percent", 0),
            "message": job.get("message", ""),
            "done_pages": job.get("done_pages", 0),
            "total_pages": job.get("total_pages", 0),
            "elapsed_seconds": elapsed_seconds,
            "elapsed_label": format_duration(elapsed_seconds),
            "eta_seconds": eta_seconds,
            "eta_label": format_duration(eta_seconds),
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
    app.run(debug=True, use_reloader=True, threaded=True, host="127.0.0.1", port=5000)
