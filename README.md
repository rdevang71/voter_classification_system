# Voter PDF Extractor

Local web app and command-line tool for extracting one voter per row from electoral-roll PDFs with OCR and exporting review-ready Excel reports.

The app does not infer caste, religion, or other sensitive traits from names. It includes blank `religion_label` and `caste_label` columns for official or manually provided labels, and the dashboard counts those labels only when present.

## Requirements

- Python 3
- Poppler for Windows:
  `C:\Release-26.02.0-0\poppler-26.02.0\Library\bin`
- Tesseract OCR:
  `C:\Program Files\Tesseract-OCR\tesseract.exe`
- Python packages:
  `pip install -r requirements.txt`

This project includes local OCR language data in `tessdata` for `hin+eng`.

If Poppler or Tesseract is installed somewhere else, set:

```powershell
$env:POPPLER_PATH = "C:\path\to\poppler\Library\bin"
$env:TESSERACT_CMD = "C:\path\to\Tesseract-OCR\tesseract.exe"
```

## Web App

```powershell
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

Upload a PDF, choose an optional page range, then download:

- `voter_extraction.xlsx`
- `dashboard_summary.xlsx`

The dashboard shows total voters, pages processed, OCR review status, and counts for provided religion/caste labels.

For the web app, uploaded PDFs are stored only in a temporary OS folder while the request is processing. They are not saved to an `uploads` folder. Generated Excel downloads are kept in memory for the running app process.

## Command Line

```powershell
python voter_classifier.py "E:\Downloads\your-voter-list.pdf" --first-page 1 --last-page 5
```

Outputs are written to `outputs`.

## Terminal PATH

The app can use Poppler directly even if `pdftoppm` is not available globally. To use `pdftoppm -v` in PowerShell, open a new terminal after the user PATH update.
