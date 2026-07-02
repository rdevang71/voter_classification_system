# Voter PDF Extractor

Web app and command-line tool for OCR processing electoral-roll PDFs and exporting Excel reports.

The app can process multiple PDFs in one batch. Pages are processed through a shared worker pool, and the web UI shows progress, elapsed time, pages completed, and estimated time left.

## What This App Does

- Upload one or more PDF electoral rolls.
- Convert PDF pages to images with Poppler.
- Run OCR with Tesseract.
- Extract voter rows into Excel.
- Generate dashboard summary Excel.
- Show live progress and ETA during processing.
- Support local use and VPS deployment.

Generated files:

- `voter_extraction.xlsx`
- `dashboard_summary.xlsx`

Uploaded PDFs are stored only in a temporary folder while processing. Generated downloads are kept in memory while the app process is running.

## Recommended VPS

Minimum:

- 2 vCPU
- 4 GB RAM
- 30 GB disk

Recommended:

- 4 vCPU
- 8 GB RAM
- 50 GB disk

Heavy use:

- 8 vCPU
- 16 GB RAM
- 100 GB disk

Start with:

```bash
VOTER_PAGE_WORKERS=4
```

Increase to `6` or `8` only if CPU and RAM are not overloaded.

## Local Windows Setup

Install Python packages:

```powershell
pip install -r requirements.txt
```

Install Poppler and Tesseract. The app currently checks these default Windows paths:

```text
C:\Release-26.02.0-0\poppler-26.02.0\Library\bin
C:\Program Files\Tesseract-OCR\tesseract.exe
```

If installed somewhere else, set:

```powershell
$env:POPPLER_PATH = "C:\path\to\poppler\Library\bin"
$env:TESSERACT_CMD = "C:\path\to\Tesseract-OCR\tesseract.exe"
```

Run locally:

```powershell
$env:VOTER_PAGE_WORKERS = "4"
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

## Command-Line Usage

Single PDF:

```powershell
python voter_classifier.py "E:\Downloads\your-voter-list.pdf" --first-page 1 --last-page 5
```

Multiple PDFs:

```powershell
python voter_classifier.py "E:\Downloads\list-1.pdf" "E:\Downloads\list-2.pdf" --output-dir outputs
```

Outputs are written to `outputs`.

## VPS Deployment Plan

These steps assume Ubuntu 22.04 or 24.04.

Replace:

- `your-domain.com` with your domain.
- `/var/www/voter_classifier` with your project path if different.

## 1. Connect To VPS

```bash
ssh root@YOUR_SERVER_IP
```

Update packages:

```bash
sudo apt update
sudo apt upgrade -y
```

## 2. Install System Dependencies

```bash
sudo apt install -y python3 python3-pip python3-venv nginx poppler-utils tesseract-ocr tesseract-ocr-hin
```

Check installs:

```bash
python3 --version
tesseract --version
pdftoppm -v
```

## 3. Upload Project

Create app folder:

```bash
sudo mkdir -p /var/www/voter_classifier
sudo chown -R $USER:$USER /var/www/voter_classifier
```

Upload files by Git, SCP, SFTP, or FileZilla.

Example with SCP from your local machine:

```bash
scp -r voter_classifier/* root@YOUR_SERVER_IP:/var/www/voter_classifier/
```

Then on the VPS:

```bash
cd /var/www/voter_classifier
```

## 4. Create Python Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install gunicorn
```

## 5. Test App Manually

```bash
cd /var/www/voter_classifier
source venv/bin/activate
export VOTER_PAGE_WORKERS=4
gunicorn -w 1 -b 127.0.0.1:5000 app:app
```

Open another SSH terminal and test:

```bash
curl http://127.0.0.1:5000
```

If HTML is returned, the app is working.

Stop the manual Gunicorn process with `Ctrl+C`.

## 6. Create Systemd Service

Create service file:

```bash
sudo nano /etc/systemd/system/voter-classifier.service
```

Paste:

```ini
[Unit]
Description=Voter PDF Extractor
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/var/www/voter_classifier
Environment="VOTER_PAGE_WORKERS=4"
Environment="OMP_THREAD_LIMIT=1"
ExecStart=/var/www/voter_classifier/venv/bin/gunicorn -w 1 -b 127.0.0.1:5000 --timeout 1800 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Set permissions:

```bash
sudo chown -R www-data:www-data /var/www/voter_classifier
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable voter-classifier
sudo systemctl start voter-classifier
sudo systemctl status voter-classifier
```

View logs:

```bash
sudo journalctl -u voter-classifier -f
```

## 7. Configure Nginx

Create Nginx config:

```bash
sudo nano /etc/nginx/sites-available/voter-classifier
```

Paste:

```nginx
server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 500M;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 1800;
        proxy_connect_timeout 1800;
        proxy_send_timeout 1800;
    }
}
```

Enable site:

```bash
sudo ln -s /etc/nginx/sites-available/voter-classifier /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

Now open:

```text
http://your-domain.com
```

## 8. Add SSL Certificate

Install Certbot:

```bash
sudo apt install -y certbot python3-certbot-nginx
```

Generate SSL:

```bash
sudo certbot --nginx -d your-domain.com
```

Open:

```text
https://your-domain.com
```

## 9. Worker Tuning

The main speed setting is:

```bash
VOTER_PAGE_WORKERS=4
```

Suggested values:

- 2 vCPU VPS: `2`
- 4 vCPU VPS: `4`
- 8 vCPU VPS: `6` or `8`
- 16 vCPU VPS: `8` to `12`

Do not set this too high. Tesseract is CPU-heavy, and too many workers can make the app slower or unstable.

To change workers:

```bash
sudo nano /etc/systemd/system/voter-classifier.service
```

Edit:

```ini
Environment="VOTER_PAGE_WORKERS=4"
```

Reload:

```bash
sudo systemctl daemon-reload
sudo systemctl restart voter-classifier
```

## 10. Update Deployment

Upload new code, then:

```bash
cd /var/www/voter_classifier
sudo chown -R www-data:www-data /var/www/voter_classifier
sudo systemctl restart voter-classifier
```

If requirements changed:

```bash
cd /var/www/voter_classifier
sudo -u www-data /var/www/voter_classifier/venv/bin/pip install -r requirements.txt
sudo systemctl restart voter-classifier
```

## 11. Troubleshooting

Check app service:

```bash
sudo systemctl status voter-classifier
```

Watch app logs:

```bash
sudo journalctl -u voter-classifier -f
```

Check Nginx:

```bash
sudo nginx -t
sudo systemctl status nginx
```

Check Nginx logs:

```bash
sudo tail -f /var/log/nginx/error.log
```

If upload fails, confirm this is in Nginx:

```nginx
client_max_body_size 500M;
```

If OCR fails, confirm:

```bash
tesseract --version
tesseract --list-langs
pdftoppm -v
```

If Hindi OCR is missing:

```bash
sudo apt install -y tesseract-ocr-hin
```

If processing is slow:

- Keep DPI at `150`.
- Use `VOTER_PAGE_WORKERS=4` on a 4 vCPU VPS.
- Do not run multiple Gunicorn workers.
- Use `gunicorn -w 1` because the app already parallelizes OCR pages internally.

If the app times out:

- Keep `--timeout 1800` in Gunicorn.
- Keep Nginx proxy timeouts at `1800`.

## 12. Security Notes

- Use HTTPS with Certbot.
- Keep the VPS updated.
- Do not expose Flask dev server directly to the internet.
- Use Nginx in front of Gunicorn.
- Consider adding login/authentication before public use.
- Uploaded PDFs are temporary, but extracted Excel files remain available in memory until the app restarts.

## 13. Useful Commands

Restart app:

```bash
sudo systemctl restart voter-classifier
```

Restart Nginx:

```bash
sudo systemctl restart nginx
```

See live app logs:

```bash
sudo journalctl -u voter-classifier -f
```

See running processes:

```bash
ps aux | grep gunicorn
```

Check disk:

```bash
df -h
```

Check memory:

```bash
free -h
```

Check CPU:

```bash
top
```
