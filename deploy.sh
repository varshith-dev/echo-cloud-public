#!/bin/bash
sudo apt-get install -y python3-flask python3-psutil python3-psycopg2
sudo rm -rf /opt/dashboard
sudo mv ~/dashboard /opt/dashboard
sudo chown -R ubuntu:ubuntu /opt/dashboard

# Create systemd service
sudo tee /etc/systemd/system/dashboard.service > /dev/null << 'EOF'
[Unit]
Description=SaaS Dashboard Backend
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/dashboard
ExecStart=/usr/bin/python3 app.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable dashboard
sudo systemctl restart dashboard

# Configure Nginx
sudo rm -f /etc/nginx/sites-enabled/default
sudo tee /etc/nginx/sites-available/dashboard > /dev/null << 'EOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/dashboard /etc/nginx/sites-enabled/
sudo systemctl restart nginx
