import sys
sys.path.append('.')
from config import Settings
from core.manifest_parser import ManifestParser
from core.metric_registry import MetricRegistry

s = Settings()
p = ManifestParser()
p.load(s.manifest_path)
r = MetricRegistry()
r.load(s.metrics_path, s.semantic_models_path, p)
for name, model in r._semantic_models.items():
    print(f"Model: {name}, Entity: {model.primary_entity}, Dimensions: {model.dimensions}")
