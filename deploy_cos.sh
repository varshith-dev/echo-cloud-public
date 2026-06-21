#!/bin/bash

# Stop and disable old dashboard
sudo systemctl stop dashboard || true
sudo systemctl disable dashboard || true

# Install Python dependencies for FastAPI
sudo apt-get update
sudo apt-get install -y python3-pip
sudo pip3 install fastapi uvicorn websockets psutil boto3

# Setup directory
sudo rm -rf /opt/cos
sudo mv ~/cos /opt/cos
sudo chown -R root:root /opt/cos

# Create COS systemd service
sudo tee /etc/systemd/system/cos.service > /dev/null << 'EOF'
[Unit]
Description=Cloud OS Backend
After=network.target

[Service]
User=root
WorkingDirectory=/opt/cos
ExecStart=/usr/bin/python3 -m uvicorn main:app --host 127.0.0.1 --port 5000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable cos
sudo systemctl restart cos

# Reconfigure Nginx to proxy websockets properly
sudo rm -f /etc/nginx/sites-enabled/dashboard
sudo tee /etc/nginx/sites-available/cos > /dev/null << 'EOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        
        # WebSocket support
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/cos /etc/nginx/sites-enabled/
sudo systemctl restart nginx
