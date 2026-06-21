import os
import pty
import fcntl
import termios
import struct
import select
import asyncio
import subprocess
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import psutil
import json
import boto3


app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def get_index():
    return FileResponse('static/index.html')

# System Stats API
@app.get("/api/system")
async def get_system_info():
    mem = psutil.virtual_memory()
    return {
        "cpu": psutil.cpu_percent(),
        "memory": {
            "total": mem.total,
            "used": mem.used,
            "percent": mem.percent
        },
        "disk": psutil.disk_usage('/').percent
    }

# File Manager API
@app.get("/api/files")
async def list_files(path: str = "/"):
    try:
        if not os.path.exists(path):
            return JSONResponse(status_code=404, content={"error": "Path not found"})
        
        items = []
        for item in os.listdir(path):
            full_path = os.path.join(path, item)
            is_dir = os.path.isdir(full_path)
            size = os.path.getsize(full_path) if not is_dir else 0
            items.append({
                "name": item,
                "path": full_path,
                "is_dir": is_dir,
                "size": size
            })
        # Sort directories first, then alphabetically
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return {"items": items, "current_path": path}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/files/read")
async def read_file(path: str):
    try:
        if not os.path.isfile(path):
            return JSONResponse(status_code=404, content={"error": "File not found"})
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        return {"content": content}
    except UnicodeDecodeError:
        return JSONResponse(status_code=400, content={"error": "Cannot read binary file"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/files/write")
async def write_file(request: Request):
    try:
        data = await request.json()
        path = data.get("path")
        content = data.get("content")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return {"status": "success"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# Cloud Storage Bucket API
CONFIG_FILE = "cloud_config.json"

def get_boto3_client():
    if not os.path.exists(CONFIG_FILE):
        return None, None
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
        client = boto3.client(
            's3',
            aws_access_key_id=config.get('access_key'),
            aws_secret_access_key=config.get('secret_key'),
            endpoint_url=config.get('endpoint_url') or None,
            region_name=config.get('region') or None
        )
        return client, config.get('bucket_name')
    except Exception:
        return None, None

@app.get("/api/bucket/config")
async def get_bucket_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
        # Hide secret key
        if 'secret_key' in config:
            config['secret_key'] = '********'
        return config
    return {}

@app.post("/api/bucket/config")
async def save_bucket_config(request: Request):
    data = await request.json()
    with open(CONFIG_FILE, 'w') as f:
        json.dump(data, f)
    return {"status": "success"}

@app.get("/api/bucket/files")
async def list_bucket_files(prefix: str = ""):
    client, bucket = get_boto3_client()
    if not client or not bucket:
        return JSONResponse(status_code=400, content={"error": "Bucket not configured"})
    try:
        if prefix == "/":
            prefix = ""
        elif prefix and not prefix.endswith('/'):
            prefix += '/'
        # Emulate directory structure
        paginator = client.get_paginator('list_objects_v2')
        items = []
        for result in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter='/'):
            for prefix_item in result.get('CommonPrefixes', []):
                name = prefix_item.get('Prefix')[len(prefix):-1]
                if name:
                    items.append({
                        "name": name,
                        "path": prefix_item.get('Prefix'),
                        "is_dir": True,
                        "size": 0
                    })
            for obj in result.get('Contents', []):
                name = obj.get('Key')[len(prefix):]
                if name: # skip the prefix directory itself
                    items.append({
                        "name": name,
                        "path": obj.get('Key'),
                        "is_dir": False,
                        "size": obj.get('Size')
                    })
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return {"items": items, "current_path": prefix}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/bucket/read")
async def read_bucket_file(path: str):
    client, bucket = get_boto3_client()
    if not client or not bucket:
        return JSONResponse(status_code=400, content={"error": "Bucket not configured"})
    try:
        response = client.get_object(Bucket=bucket, Key=path)
        content = response['Body'].read().decode('utf-8')
        return {"content": content}
    except UnicodeDecodeError:
        return JSONResponse(status_code=400, content={"error": "Cannot read binary file"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/bucket/write")
async def write_bucket_file(request: Request):
    client, bucket = get_boto3_client()
    if not client or not bucket:
        return JSONResponse(status_code=400, content={"error": "Bucket not configured"})
    try:
        data = await request.json()
        path = data.get("path")
        content = data.get("content")
        client.put_object(Bucket=bucket, Key=path, Body=content.encode('utf-8'))
        return {"status": "success"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# Services API
@app.get("/api/services")
async def get_services():
    services = ["nginx", "postgresql", "cos"]
    status_list = []
    for srv in services:
        try:
            result = subprocess.run(["systemctl", "is-active", srv], capture_output=True, text=True)
            status = "Running" if result.stdout.strip() == "active" else "Stopped"
        except:
            status = "Unknown"
        status_list.append({"name": srv, "status": status})
    return {"services": status_list}

@app.post("/api/services/action")
async def service_action(request: Request):
    data = await request.json()
    action = data.get("action")
    service = data.get("service")
    if action not in ["start", "stop", "restart"]:
        return JSONResponse(status_code=400, content={"error": "Invalid action"})
    try:
        subprocess.run(["sudo", "systemctl", action, service], check=True)
        return {"status": "success"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# Domain API (Nginx)
@app.get("/api/domains")
async def get_domains():
    try:
        sites_path = "/etc/nginx/sites-available"
        if os.path.exists(sites_path):
            domains = [f for f in os.listdir(sites_path) if f != "default"]
            return {"domains": domains}
        return {"domains": []}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/domains")
async def add_domain(request: Request):
    data = await request.json()
    domain = data.get("domain")
    port = data.get("port", 80)
    if not domain:
        return JSONResponse(status_code=400, content={"error": "Domain required"})
    
    conf = f"""
server {{
    listen 80;
    server_name {domain};
    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_set_header Host $host;
    }}
}}
"""
    try:
        conf_path = f"/etc/nginx/sites-available/{domain}"
        link_path = f"/etc/nginx/sites-enabled/{domain}"
        with open(conf_path, 'w') as f:
            f.write(conf)
        if not os.path.exists(link_path):
            os.symlink(conf_path, link_path)
        subprocess.run(["sudo", "systemctl", "reload", "nginx"])
        return {"status": "success"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# Web Terminal WebSocket
@app.websocket("/ws/terminal")
async def terminal_endpoint(websocket: WebSocket):
    await websocket.accept()
    pid, fd = pty.fork()
    if pid == 0:
        os.environ['TERM'] = 'xterm-256color'
        os.execvp('bash', ['bash'])
    else:
        def set_winsize(fd, row, col):
            winsize = struct.pack("HHHH", row, col, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
            
        loop = asyncio.get_running_loop()
        
        async def read_from_pty():
            try:
                while True:
                    data = await loop.run_in_executor(None, os.read, fd, 1024)
                    if not data: break
                    await websocket.send_bytes(data)
            except: pass
                
        read_task = asyncio.create_task(read_from_pty())
        
        try:
            while True:
                data = await websocket.receive_text()
                if data.startswith('RESIZE:'):
                    try:
                        _, dims = data.split(':')
                        cols, rows = map(int, dims.split(','))
                        set_winsize(fd, rows, cols)
                    except: pass
                else:
                    os.write(fd, data.encode('utf-8'))
        except WebSocketDisconnect: pass
        finally:
            read_task.cancel()
            try: os.close(fd)
            except: pass
            try: os.waitpid(pid, 0)
            except: pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=False)
