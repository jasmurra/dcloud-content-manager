#!/usr/bin/env python3
"""Redacted summary of CAI VM-promotion HAR (no cookies/tokens/bodies)."""
import json
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HAR = Path("/Users/jasmurra/Downloads/vmpromotion-take2.har")
OUT = Path("/Users/jasmurra/Desktop/scripting/dcloud-content-manager/har_cai_summary.json")

SKIP_EXT = (".js", ".css", ".png", ".jpg", ".svg", ".woff", ".woff2", ".ico", ".map", ".gif", ".ttf")
SECRET = ("cookie", "authorization", "set-cookie", "token", "csrf", "xsrf", "auth")


def keys_of(obj, depth=0):
    if depth > 4 or obj is None:
        return []
    if isinstance(obj, dict):
        return sorted(str(k) for k in obj.keys())[:60]
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return keys_of(obj[0], depth + 1)
    return [type(obj).__name__]


def main():
    har = json.loads(HAR.read_text(encoding="utf-8"))
    rows = []
    for entry in har.get("log", {}).get("entries", []):
        req = entry.get("request") or {}
        resp = entry.get("response") or {}
        url = req.get("url") or ""
        parsed = urlparse(url)
        path = parsed.path
        if path.lower().endswith(SKIP_EXT):
            continue
        host = parsed.netloc
        if not any(x in host for x in ("dcloud", "cai", "camgr", "cisco.com")):
            continue
        if "duo" in host or "amplitude" in host:
            continue
        post = req.get("postData") or {}
        post_text = post.get("text") or ""
        post_keys = []
        post_preview = ""
        if post_text.strip().startswith(("{", "[")):
            try:
                parsed_post = json.loads(post_text)
                post_keys = keys_of(parsed_post)
            except json.JSONDecodeError:
                post_keys = ["<non-json>"]
        elif post_text:
            # form body: keep field names only
            names = []
            for part in post_text.split("&"):
                name = part.split("=", 1)[0]
                if name and "token" not in name.lower() and "cookie" not in name.lower():
                    names.append(name)
            post_keys = names[:40]
            post_preview = "form:" + ",".join(names[:20])
        body = (resp.get("content") or {}).get("text") or ""
        body_keys = []
        snippet = ""
        mime = (resp.get("content") or {}).get("mimeType") or ""
        if "json" in mime or body.strip().startswith(("{", "[")):
            try:
                data = json.loads(body)
                body_keys = keys_of(data)
                if isinstance(data, dict):
                    snippet = {k: data[k] for k in list(data)[:12] if not isinstance(data[k], (dict, list))}
                    # include list lengths
                    for k, v in data.items():
                        if isinstance(v, list):
                            snippet[k] = f"list[{len(v)}]"
                        elif isinstance(v, dict):
                            snippet[k] = f"dict keys={list(v)[:8]}"
                elif isinstance(data, list) and data:
                    snippet = f"list[{len(data)}] item_keys={keys_of(data[0])}"
            except json.JSONDecodeError:
                body_keys = ["<non-json>"]
                snippet = body[:180].replace("\n", " ")
        elif "html" in mime:
            snippet = "html"
        header_names = [h.get("name") for h in req.get("headers") or []]
        authish = [n for n in header_names if any(s in (n or "").lower() for s in SECRET)]
        rows.append({
            "method": req.get("method"),
            "status": resp.get("status"),
            "host": host,
            "path": path,
            "query_keys": sorted(parse_qs(parsed.query).keys()),
            "auth_headers": [f"{n} (present)" for n in authish],
            "post_mime": post.get("mimeType") or "",
            "post_keys": post_keys,
            "post_preview": post_preview,
            "resp_mime": mime,
            "resp_keys": body_keys,
            "resp_snippet": snippet,
            "resp_bytes": len(body),
        })
    OUT.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} rows")


if __name__ == "__main__":
    main()
