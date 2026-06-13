import json
import logging

logging.basicConfig(level=logging.INFO)

def build_metric_aware_dimension_map():
    with open('../dbt_streaming_analytics/streaming_analytics/target/semantic_manifest.json') as f:
        manifest = json.load(f)
        
    # 1. Map measure -> semantic model
    measure_to_sm = {}
    for sm in manifest.get('semantic_models', []):
        for measure in sm.get('measures', []):
            measure_to_sm[measure['name']] = sm
            
    # 2. Extract all dimensions and their prefixes globally
    # bare_dim -> list of prefixes
    global_dims = {}
    for sm in manifest.get('semantic_models', []):
        entities = [e['name'] for e in sm.get('entities', []) if e.get('type') in ('primary', 'foreign')]
        for dim in sm.get('dimensions', []):
            dim_name = dim['name']
            is_time = dim.get('type') == 'time'
            granularity = dim['type_params'].get('time_granularity') if is_time and dim.get('type_params') else None
            
            for entity in entities:
                prefixed = f"{entity}__{dim_name}__{granularity}" if is_time and granularity else f"{entity}__{dim_name}"
                if dim_name not in global_dims:
                    global_dims[dim_name] = []
                if prefixed not in global_dims[dim_name]:
                    global_dims[dim_name].append(prefixed)
                    
    # 3. For each metric, resolve the best prefix for every dimension
    metric_map = {}
    for metric in manifest.get('metrics', []):
        m_name = metric['name']
        
        # Find which semantic models this metric uses
        input_measures = metric.get('type_params', {}).get('input_measures', [])
        used_sms = []
        for im in input_measures:
            sm = measure_to_sm.get(im['name'])
            if sm and sm not in used_sms:
                used_sms.append(sm)
                
        # Primary entities of the used semantic models
        primary_entities = []
        for sm in used_sms:
            for e in sm.get('entities', []):
                if e.get('type') == 'primary':
                    primary_entities.append(e['name'])
                    
        # Now map all dimensions for this metric
        m_dim_map = {}
        for dim_name, prefixes in global_dims.items():
            if len(prefixes) == 1:
                m_dim_map[dim_name] = prefixes[0]
            else:
                # Multiple prefixes exist (e.g. plan_type -> subscription__plan_type, subscriber__plan_type)
                # Heuristic: 
                # 1. If any prefix starts with a primary entity of this metric, use it.
                # 2. User specific overrides based on metric name
                chosen = None
                
                # Override rules from user
                if m_name in ['total_subscribers', 'churned_subscribers', 'ltv', 'churn_rate']:
                    for p in prefixes:
                        if p.startswith('subscriber__'):
                            chosen = p
                            break
                elif m_name in ['mrr', 'expansion_mrr']:
                    for p in prefixes:
                        if p.startswith('subscription__'):
                            chosen = p
                            break
                elif m_name in ['engagement_rate', 'recommendation_ctr']:
                    for p in prefixes:
                        if p.startswith('session__') or p.startswith('event__'):
                            chosen = p
                            break
                            
                # Fallback to primary entity match
                if not chosen:
                    for p in prefixes:
                        if any(p.startswith(pe + '__') for pe in primary_entities):
                            chosen = p
                            break
                            
                # Ultimate fallback to first
                if not chosen:
                    chosen = prefixes[0]
                    
                m_dim_map[dim_name] = chosen
                
        metric_map[m_name] = m_dim_map
        
    return metric_map

if __name__ == "__main__":
    m = build_metric_aware_dimension_map()
    print("MRR plan_type:", m['mrr']['plan_type'])
    print("LTV plan_type:", m['ltv']['plan_type'])
    print("CHURN plan_type:", m['churn_rate']['plan_type'])
