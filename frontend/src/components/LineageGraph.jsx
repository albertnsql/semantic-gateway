/**
 * components/LineageGraph.jsx — horizontal node chain showing dbt lineage.
 * Props:
 *   path: string[]  — ordered list of model names (left to right)
 *   steps: [{model_name, layer, description}]  — optional enriched step data
 */
import { Database, GitBranch, BarChart3, Target, Layers } from 'lucide-react';

const CLAY_BTN = `12px 12px 24px rgba(13,148,136,0.20), -8px -8px 16px rgba(255,255,255,0.8), inset 4px 4px 8px rgba(255,255,255,0.6), inset -4px -4px 8px rgba(13,148,136,0.06)`;

const LAYER_META = {
  raw:          { icon: Database,  gradient: 'from-sky-400 to-sky-600',     ring: 'rgba(14,165,233,0.25)' },
  staging:      { icon: GitBranch, gradient: 'from-cyan-400 to-cyan-600',   ring: 'rgba(6,182,212,0.25)'  },
  intermediate: { icon: Layers,    gradient: 'from-amber-400 to-amber-600', ring: 'rgba(245,158,11,0.25)' },
  marts:        { icon: BarChart3, gradient: 'from-teal-400 to-teal-600',   ring: 'rgba(13,148,136,0.25)' },
  metric:       { icon: Target,    gradient: 'from-teal-500 to-emerald-500',ring: 'rgba(13,148,136,0.40)' },
};

function inferLayer(name) {
  if (name.startsWith('stg_'))  return 'staging';
  if (name.startsWith('int_'))  return 'intermediate';
  if (name.startsWith('fct_') || name.startsWith('dim_')) return 'marts';
  if (name.startsWith('raw.') || !name.includes('_'))     return 'raw';
  return 'staging';
}

function LineageNode({ name, layer, isLast }) {
  const meta = LAYER_META[layer] ?? LAYER_META.staging;
  const Icon = meta.icon;

  return (
    <div className="flex items-center gap-3 shrink-0">
      <div
        className="flex flex-col items-center gap-2 px-4 py-3 rounded-[24px] min-w-[110px] max-w-[140px]
                   backdrop-blur-xl transition-all duration-300 hover:-translate-y-1"
        style={{
          background: isLast
            ? 'linear-gradient(135deg, rgba(240,253,250,0.95), rgba(255,255,255,0.98))'
            : 'rgba(255,255,255,0.70)',
          boxShadow: isLast
            ? `20px 20px 40px rgba(13,148,136,0.18), -12px -12px 28px rgba(255,255,255,0.95), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`
            : CLAY_BTN,
        }}
      >
        {/* Icon orb */}
        <div
          className={`w-10 h-10 rounded-full flex items-center justify-center bg-gradient-to-br ${meta.gradient} ${isLast ? 'animate-clay-breathe' : ''}`}
          style={{
            boxShadow: `6px 6px 12px ${meta.ring}, -4px -4px 8px rgba(255,255,255,0.9)`,
          }}
        >
          <Icon size={16} className="text-white" />
        </div>
        <span
          className="text-xs font-mono text-center leading-tight break-all font-bold"
          style={{ color: isLast ? '#0D9488' : '#1A3A38', fontFamily: 'JetBrains Mono, monospace' }}
        >
          {name}
        </span>
        <span
          className="text-[9px] font-bold uppercase tracking-widest px-2 py-0.5 rounded-full"
          style={{
            color: isLast ? '#0D9488' : '#4A7B76',
            background: isLast ? 'rgba(13,148,136,0.10)' : 'rgba(74,123,118,0.08)',
            boxShadow: 'inset 1px 1px 3px rgba(255,255,255,0.8), inset -1px -1px 3px rgba(13,148,136,0.06)',
            fontFamily: 'DM Sans, sans-serif',
          }}
        >
          {layer}
        </span>
      </div>
      {!isLast && (
        <span className="text-[#4A7B76]/40 text-lg font-mono select-none">→</span>
      )}
    </div>
  );
}

export default function LineageGraph({ path = [], steps = [] }) {
  if (!path || path.length === 0) {
    return (
      <p
        className="text-[#4A7B76] text-sm text-center py-8"
        style={{ fontFamily: 'DM Sans, sans-serif' }}
      >
        No lineage path available.
      </p>
    );
  }

  // Build a layer map from enriched steps if available
  const layerMap = {};
  steps.forEach((s) => { layerMap[s.model_name] = s.layer; });

  return (
    <div className="overflow-x-auto pb-3">
      <div className="flex items-center gap-1 min-w-max py-2">
        {path.map((name, idx) => {
          const isLast = idx === path.length - 1;
          const layer  = layerMap[name] ?? (isLast ? 'metric' : inferLayer(name));
          return <LineageNode key={`${name}-${idx}`} name={name} layer={layer} isLast={isLast} />;
        })}
      </div>
    </div>
  );
}
