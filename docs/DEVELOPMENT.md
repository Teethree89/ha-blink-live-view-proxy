# Development

## Validate

From this folder:

```bash
python3 -m py_compile custom_components/blink_liveview_proxy/*.py
python3 -m py_compile proxy/blink_liveview_proxy.py
node --check custom_components/blink_liveview_proxy/frontend/blink-liveview-dialog.js
```

## Local Proxy Run

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r proxy/requirements.txt
cp proxy/config.example.json proxy/config.json
python proxy/blink_liveview_proxy.py --config proxy/config.json list
python proxy/blink_liveview_proxy.py --config proxy/config.json serve
```

## Endpoint Smoke Tests

```bash
curl http://127.0.0.1:8088/health
curl http://127.0.0.1:8088/cameras
curl http://127.0.0.1:8088/clips?source=local&hours=24&limit=5
```

## Home Assistant Static Asset

The player loads:

```text
/api/blink_liveview_proxy/static/mpegts.min.js
```

Dashboards should load:

```text
/api/blink_liveview_proxy/static/blink-liveview-dialog.js
```

That keeps the frontend helper inside the custom integration instead of
requiring a separate `/config/www` copy.
