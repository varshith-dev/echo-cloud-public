#!/bin/bash
sudo -u postgres psql -c "CREATE DATABASE oqens;"
sudo -u postgres psql -c "CREATE USER oqens_user WITH PASSWORD 'oqens_pass';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE oqens TO oqens_user;"
sudo -u postgres psql -d oqens -c "GRANT ALL ON SCHEMA public TO oqens_user;"
