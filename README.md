# OQENS — Multi-Tenant Cloud Storage SaaS Platform

OQENS is a high-performance, multi-tenant cloud storage and workspace platform. It provides tenants with isolated S3-compatible backend storage, a web-based dashboard, support ticketing system, billing management, and document collections.

---

## Core Features

- **Isolated Multi-Tenant Storage:** Virtual directories and access boundaries ensure complete isolation between tenant environments sharing backend S3 buckets.
- **Unified Web Dashboard:** Clean, responsive, and minimalist frontend interface built with vanilla HTML/CSS and JavaScript.
- **Custom Domain Routing:** Support for assigning and verifying custom domains dynamically to tenants.
- **Integrated Support Ticketing:** Dedicated support channels with threads and poll capabilities.
- **Subscription Billing:** Workspace renewal management with payment verification integration.
- **Markdown Collections:** Organization and preview support for markdown documentation.

---

## Architecture Overview

- **Frontend:** Pure HTML5 and Vanilla CSS/JS. Uses dynamic client-side filtering, sorting, and tag management.
- **Backend:** Flask (Python 3) engine handling S3 operations via Boto3, tenant sessions, scheduling, and billing routines.
- **Database:** PostgreSQL or SQLite via dynamic DB connection wrapper.
- **Reverse Proxy:** Nginx proxying SSL termination and virtual hosts.

---

## Running Locally

To run the OQENS backend locally for development or testing:

1. **Clone the repository:**
   ```bash
   git clone https://github.com/varshith-dev/echo-cloud-public.git
   cd echo-cloud-public
   ```

2. **Set up a Python virtual environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
   *(Or manual install: `pip install flask boto3 psutil` depending on environment)*

4. **Start the Flask server:**
   ```bash
   cd dashboard
   python app.py
   ```
   The application will be accessible locally at `http://localhost:5000`.

---

## Development Guidelines

- **Minimalist Styling:** Do not add heavy CSS frameworks (such as Tailwind or Bootstrap) unless explicitly specified. Focus on vanilla styling.
- **Strict Data Isolation:** Prefix S3 storage keys with `tenant_<username>/` to guarantee separation.
- **API Cleanliness:** Keep endpoint structures consistent and avoid exposing unnecessary backend configuration details.
