"""Smoke test all gateway endpoints."""
import urllib.request
import json

base = "http://localhost:8000"

def get(path, timeout=10):
    try:
        res = urllib.request.urlopen(base + path, timeout=timeout)
        return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
    except Exception as e:
        return None, str(e)

print("=" * 60)
print("  AI Semantic Gateway — Live Endpoint Verification")
print("=" * 60)

# 1. Health
status, data = get("/api/v1/health")
if status == 200:
    print(f"\n[PASS] GET /api/v1/health -> {status}")
    print(f"       overall_status  : {data.get('status')}")
    print(f"       manifest_loaded : {data.get('manifest_loaded')}")
    print(f"       metrics_loaded  : {data.get('metrics_loaded')}")
    print(f"       sem_models      : {data.get('semantic_models_loaded')}")
    print(f"       env             : {data.get('gateway_env')}")
    print(f"       version         : {data.get('gateway_version')}")
else:
    print(f"\n[FAIL] GET /api/v1/health -> {status}: {data}")

# 2. Metrics catalog
status, data = get("/api/v1/metrics")
if status == 200:
    names = [m["name"] for m in data]
    print(f"\n[PASS] GET /api/v1/metrics -> {status}")
    print(f"       total certified metrics : {len(data)}")
    for m in data:
        print(f"       [{m['metric_type']:>8}] {m['name']:30} -> {m['source_model']}")
else:
    print(f"\n[FAIL] GET /api/v1/metrics -> {status}: {data}")

# 3. Single metric detail
status, data = get("/api/v1/metrics/mrr")
if status == 200:
    print(f"\n[PASS] GET /api/v1/metrics/mrr -> {status}")
    print(f"       label      : {data.get('label')}")
    print(f"       grain      : {data.get('grain')}")
    print(f"       source     : {data.get('source_model')}")
    print(f"       dims ({len(data.get('certified_dimensions', []))}): {data.get('certified_dimensions', [])[:4]}")
else:
    print(f"\n[FAIL] GET /api/v1/metrics/mrr -> {status}: {data}")

# 4. Unknown metric 404
status, data = get("/api/v1/metrics/made_up_metric_xyz")
if status == 404:
    print(f"\n[PASS] GET /api/v1/metrics/made_up_metric_xyz -> {status} (404 expected)")
else:
    print(f"\n[FAIL] Expected 404, got {status}: {data}")

# 5. Lineage
status, data = get("/api/v1/lineage/mrr")
if status == 200:
    steps = [s["model_name"] for s in data.get("transformation_steps", [])]
    print(f"\n[PASS] GET /api/v1/lineage/mrr -> {status}")
    print(f"       source_model    : {data.get('source_model')}")
    print(f"       upstream_models : {data.get('upstream_models')}")
    print(f"       source_tables   : {data.get('source_tables')}")
    print(f"       lineage path    : {' -> '.join(steps)}")
else:
    print(f"\n[FAIL] GET /api/v1/lineage/mrr -> {status}: {data}")

# 6. OpenAPI schema (verify all routes registered)
status, data = get("/openapi.json", timeout=5)
if status == 200:
    paths = list(data.get("paths", {}).keys())
    print(f"\n[PASS] GET /openapi.json -> {status}")
    print(f"       registered routes: {paths}")
else:
    print(f"\n[FAIL] GET /openapi.json -> {status}")

print("\n" + "=" * 60)
print("  Verification complete")
print("=" * 60)
