import urllib.request
import re

url = "https://imgfy.net/image/VOc"
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
try:
    with urllib.request.urlopen(req, timeout=10) as response:
        content = response.read()
        content_type = response.headers.get('Content-Type', '')
        print("Content-Type:", content_type)
        if 'text/html' in content_type:
            html = content.decode('utf-8', errors='ignore')
            print("Found HTML. Searching for image tags...")
            # Search og:image
            match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
            if match:
                print("og:image found:", match.group(1))
            else:
                # search for typical img tag that might be the main image
                print("No og:image. Some HTML snippet:", html[:500])
                for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE):
                    print("Found img src:", m.group(1))
except Exception as e:
    print("Error:", e)
