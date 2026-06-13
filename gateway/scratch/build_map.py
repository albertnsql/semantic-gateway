import json
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def build_dimension_prefix_map(manifest_path):
    with open(manifest_path, 'r') as f:
        manifest = json.load(f)
    
    dim_map = {}
    for sm in manifest.get('semantic_models', []):
        entities = [e['name'] for e in sm.get('entities', []) if e.get('type') in ('primary', 'foreign')]
        
        for dim in sm.get('dimensions', []):
            dim_name = dim['name']
            is_time = dim.get('type') == 'time'
            granularity = None
            if is_time and dim.get('type_params'):
                granularity = dim['type_params'].get('time_granularity')
            
            for entity in entities:
                if is_time and granularity:
                    prefixed = f"{entity}__{dim_name}__{granularity}"
                else:
                    prefixed = f"{entity}__{dim_name}"
                
                if dim_name not in dim_map:
                    dim_map[dim_name] = []
                if prefixed not in dim_map[dim_name]:
                    dim_map[dim_name].append(prefixed)
                    
    final_map = {}
    for bare_name, prefixes in dim_map.items():
        if len(prefixes) > 1:
            logger.warning("Dimension '%s' has multiple join paths: %s. Using default: %s", bare_name, prefixes, prefixes[0])
        final_map[bare_name] = prefixes[0]
        
    logger.info("Dimension prefix map built: %d mappings loaded", len(final_map))
    return final_map

if __name__ == "__main__":
    m = build_dimension_prefix_map('../dbt_streaming_analytics/streaming_analytics/target/semantic_manifest.json')
    for k, v in m.items():
        print(f'"{k}": "{v}",')
