# VPS Deployment Plan

Deploy this Flask OCR app on Ubuntu 22.04 or 24.04 using Gunicorn, systemd, and Nginx.

Replace:

- `YOUR_SERVER_IP` with your VPS IP.
- `your-domain.com` with your domain.
- `/var/www/voter_classifier` if you use another folder.

## 1. Install Server Packages

```bash
ssh root@YOUR_SERVER_IP
sudo apt update
sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv nginx poppler-utils tesseract-ocr tesseract-ocr-hin
```

Check:

```bash
tesseract --list-langs
pdftoppm -v
```

Make sure `hin` is listed.

## 2. Upload Project

```bash
sudo mkdir -p /var/www/voter_classifier
sudo chown -R $USER:$USER /var/www/voter_classifier
```

Upload project files to:

```text
/var/www/voter_classifier
```

Then:

```bash
cd /var/www/voter_classifier
```

## 3. Create Python Environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install gunicorn
```

## 4. Test Gunicorn

```bash
cd /var/www/voter_classifier
source venv/bin/activate
VOTER_PAGE_WORKERS=12 OMP_THREAD_LIMIT=2 gunicorn -w 1 -b 127.0.0.1:5000 --timeout 1800 app:app
```

In another terminal:

```bash
curl http://127.0.0.1:5000
```

Stop Gunicorn with `Ctrl+C`.

## 5. Create Systemd Service

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
Environment="VOTER_PAGE_WORKERS=12"
Environment="OMP_THREAD_LIMIT=2"
ExecStart=/var/www/voter_classifier/venv/bin/gunicorn -w 1 -b 127.0.0.1:5000 --timeout 1800 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Start service:

```bash
sudo chown -R www-data:www-data /var/www/voter_classifier
sudo systemctl daemon-reload
sudo systemctl enable voter-classifier
sudo systemctl start voter-classifier
sudo systemctl status voter-classifier
```

## 6. Configure Nginx

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

Enable:

```bash
sudo ln -s /etc/nginx/sites-available/voter-classifier /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

Open:

```text
http://your-domain.com
```

## 7. Add HTTPS

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

Open:

```text
https://your-domain.com
```

## 8. Update App Later

Upload new code, then:

```bash
cd /var/www/voter_classifier
sudo chown -R www-data:www-data /var/www/voter_classifier
sudo systemctl restart voter-classifier
```

If `requirements.txt` changed:

```bash
sudo -u www-data /var/www/voter_classifier/venv/bin/pip install -r /var/www/voter_classifier/requirements.txt
sudo systemctl restart voter-classifier
```

## 9. Useful Commands

```bash
sudo systemctl status voter-classifier
sudo journalctl -u voter-classifier -f
sudo systemctl restart voter-classifier
sudo nginx -t
sudo systemctl restart nginx
```

## 10. Worker Setting

Use:

```bash
VOTER_PAGE_WORKERS=12
```