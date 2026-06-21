# OQENS-DB: Enterprise Cloud Storage SaaS Guide

Welcome to the definitive guide for **OQENS-DB**. While the name originally implied a traditional relational database, the platform has evolved into its true purpose: a highly efficient, multi-tenant **Cloud Object Storage Platform**.

## Platform Overview

OQENS-DB acts as a lightweight, lightning-fast proxy layer over standard S3-compatible cloud storage (such as Oracle Cloud Object Storage, AWS S3, or MinIO). It brings Enterprise-grade features to standard buckets:

- **Multi-Tenancy:** Secure isolation. Multiple users/tenants can share a single Master Cloud Bucket without ever seeing each other's files.
- **Quota Management:** Hard limits on storage capacity dynamically tracked via a lightweight local SQLite database.
- **Media Previews & Sharing:** Built-in tools for generating secure, temporary Presigned URLs to share files publicly via the `dl.oqens.me` domain.

## Architecture

1. **Frontend (The Interface):** Pure HTML/Vanilla JS ensuring zero-bloat and instantaneous load times. Features Drag & Drop, native dark mode, and responsive layouts.
2. **Backend (The Engine):** Flask & Python 3. Handles S3 operations using `boto3`.
3. **Database (State & Quotas):** A local `oqens.db` SQLite database strictly tracks tenant credentials, quotas, and session states. The actual files are stored securely in the Cloud Bucket.
4. **Proxy (Nginx):** Nginx serves as the reverse proxy handling SSL and subdomain routing.

## Subdomain Routing Topology

- **`echo.oqens.me`**
  - The public face. Unauthenticated users see the minimalist landing page. Tenants click "Open Dashboard" to access their isolated Storage Explorer.
- **`host.echo.oqens.me`**
  - The hidden command center. Requires the Master Admin Code to enter. Used for configuring the Master S3 Bucket and provisioning new tenants.
- **`dl.oqens.me`**
  - The delivery network. Automatically maps `dl.oqens.me/tenant/file` to secure, expiring Presigned URLs for public media sharing.

## Deployment Checklist

1. **Environment:** Ubuntu 22.04 LTS (Oracle Cloud VM)
2. **Packages:** `nginx`, `python3-flask`, `python3-boto3`, `python3-psutil`
3. **Permissions:** 
   - `chmod +x deploy.sh`
   - Ensure Nginx SSL certificates (`/etc/letsencrypt/live/echo.oqens.me/`) cover all subdomains (`host.echo`, `dl`).
4. **Services:**
   - `sudo systemctl start dashboard`
   - `sudo systemctl restart nginx`

## For AI Agents

When modifying this repository, strictly adhere to the following rules:
- **No Heavy Frameworks:** Do not introduce React, Vue, or Tailwind unless explicitly required. Rely on Vanilla CSS for the minimalist aesthetic.
- **Boto3 is King:** All storage operations must go through the `boto3` client against the Master S3 Bucket. Do not attempt to save files locally on the VM's disk.
- **Tenant Isolation:** Every S3 key must strictly be prefixed with `tenant_<username>/` to guarantee data segregation.
