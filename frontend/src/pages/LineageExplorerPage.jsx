/**
 * pages/LineageExplorerPage.jsx — Claymorphism lineage explorer.
 */
import { useState, useEffect, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import { GitBranch, ChevronDown } from 'lucide-react';
import TopBar from '../components/TopBar';
import LineageGraph from '../components/LineageGraph';
import StatusBadge from '../components/StatusBadge';
import LoadingSpinner from '../components/LoadingSpinner';
import ErrorState from '../components/ErrorState';
import { getMetrics, getLineage } from '../api/metrics';

const CLAY_SHADOW = `16px 16px 32px rgba(13,148,136,0.12), -10px -10px 24px rgba(255,255,255,0.9), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;
const CLAY_INSET  = `inset 8px 8px 16px rgba(13,148,136,0.08), inset -8px -8px 16px rgba(255,255,255,0.9)`;
const CLAY_SHADOW_PRIMARY = `12px 12px 24px rgba(13, 148, 136, 0.25), -8px -8px 16px rgba(255, 255, 255, 0.5), inset 4px 4px 8px rgba(255, 255, 255, 0.3), inset -4px -4px 8px rgba(0, 0, 0, 0.1)`;

function inferLayer(name, isLast) {
  if (isLast)              return 'metric';
  if (name.startsWith('stg_'))  return 'staging';
  if (name.startsWith('int_'))  return 'intermediate';
  if (name.startsWith('fct_') || name.startsWith('dim_')) return 'marts';
  return 'raw';
}

function CustomDropdown({ metrics, selected, onSelect }) {
  const [isOpen, setIsOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    function handleClickOutside(e) {
      if (ref.current && !ref.current.contains(e.target)) {
        setIsOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const selectedMetric = metrics.find(m => m.name === selected);

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center justify-between rounded-[20px] px-6 py-4
                   text-white text-sm cursor-pointer transition-all duration-200
                   focus:outline-none focus:ring-4 focus:ring-[#0D9488]/30"
        style={{
          background: 'linear-gradient(135deg, #2DD4BF, #0D9488)',
          boxShadow: CLAY_SHADOW_PRIMARY,
          fontFamily: 'DM Sans, sans-serif',
        }}
      >
        <span>
          {selectedMetric ? (
            <><span className="font-bold text-white">{selectedMetric.name}</span> <span className="text-white/80">— {selectedMetric.label}</span></>
          ) : '— Choose a metric —'}
        </span>
        <ChevronDown
          size={18}
          className={`text-white transition-transform duration-300 ${isOpen ? 'rotate-180' : ''}`}
        />
      </button>

      {isOpen && (
        <div 
          className="absolute z-50 top-full left-0 right-0 mt-4 p-3 rounded-[32px] backdrop-blur-2xl animate-fade-in origin-top"
          style={{ background: 'rgba(255,255,255,0.90)', boxShadow: CLAY_SHADOW, border: '1px solid rgba(255,255,255,0.6)' }}
        >
          <div className="max-h-[300px] overflow-y-auto pr-2 custom-scrollbar flex flex-col gap-1">
            {metrics.map(m => (
              <button
                key={m.name}
                type="button"
                onClick={() => {
                  onSelect(m.name);
                  setIsOpen(false);
                }}
                className={`w-full text-left px-5 py-3.5 rounded-[20px] text-sm transition-all duration-200 flex items-center gap-2 ${
                  m.name === selected 
                    ? 'bg-[#0D9488]/10 text-[#0D9488] font-bold shadow-sm' 
                    : 'text-[#1A3A38] hover:bg-[#E6F7F6]/70 hover:-translate-y-0.5 hover:shadow-sm'
                }`}
                style={{ fontFamily: 'DM Sans, sans-serif' }}
              >
                <span className="font-bold">{m.name}</span>
                <span className={`text-xs ${m.name === selected ? 'text-[#0D9488]/70' : 'text-[#4A7B76]'}`}>— {m.label}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default function LineageExplorerPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [metrics, setMetrics]           = useState([]);
  const [selected, setSelected]         = useState(searchParams.get('metric') ?? '');
  const [lineage, setLineage]           = useState(null);
  const [metricsLoading, setMetricsLoading] = useState(true);
  const [lineageLoading, setLineageLoading] = useState(false);
  const [metricsError, setMetricsError]     = useState(null);
  const [lineageError, setLineageError]     = useState(null);

  useEffect(() => {
    getMetrics()
      .then(setMetrics)
      .catch(setMetricsError)
      .finally(() => setMetricsLoading(false));
  }, []);

  // Auto-select first metric if none is selected
  useEffect(() => {
    if (!selected && metrics.length > 0) {
      setSelected(metrics[0].name);
      setSearchParams({ metric: metrics[0].name }, { replace: true });
    }
  }, [metrics, selected, setSearchParams]);

  useEffect(() => {
    if (!selected) return;
    setLineage(null);
    setLineageError(null);
    setLineageLoading(true);
    getLineage(selected)
      .then(setLineage)
      .catch(setLineageError)
      .finally(() => setLineageLoading(false));
  }, [selected]);

  const handleSelect = (name) => {
    setSelected(name);
    if (name) setSearchParams({ metric: name });
    else setSearchParams({});
  };

  const lineagePath = lineage?.transformation_steps
    ? [
        ...(lineage.source_tables ?? []),
        ...lineage.transformation_steps.map((s) => s.model_name),
        lineage.metric_name,
      ]
    : null;

  const steps = lineage?.transformation_steps ?? [];

  return (
    <div className="max-w-5xl mx-auto flex flex-col gap-6 animate-fade-in pb-12">
      <TopBar title="Lineage Explorer" breadcrumb={['Gateway', 'Lineage']} />

      {/* Metric selector */}
      <div
        className="p-6 rounded-[32px] backdrop-blur-xl relative z-20"
        style={{ background: 'rgba(255,255,255,0.65)', boxShadow: CLAY_SHADOW }}
      >
        <label
          className="text-base font-bold text-[#1A3A38] tracking-tight block mb-4"
          style={{ fontFamily: 'Nunito, sans-serif' }}
        >
          Select a Metric
        </label>
        {metricsLoading ? (
          <LoadingSpinner label="Loading metrics…" />
        ) : metricsError ? (
          <ErrorState message="Could not load metrics list" detail={metricsError.message} />
        ) : (
          <CustomDropdown 
            metrics={metrics} 
            selected={selected} 
            onSelect={handleSelect} 
          />
        )}
      </div>

      {lineageLoading && <LoadingSpinner label={`Resolving lineage for '${selected}'…`} />}
      {lineageError  && <ErrorState message={`Failed to load lineage for '${selected}'`} detail={lineageError.message} />}

      {lineage && !lineageLoading && (
        <div className="flex flex-col gap-5 animate-slide-in relative z-10">
          {/* Summary cards */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div
              className="p-5 rounded-[32px] backdrop-blur-xl"
              style={{ background: 'rgba(255,255,255,0.65)', boxShadow: CLAY_SHADOW }}
            >
              <span className="text-xs text-[#4A7B76] font-bold uppercase tracking-wide block mb-2" style={{ fontFamily: 'DM Sans, sans-serif' }}>Metric</span>
              <code className="text-sm text-[#0D9488] font-mono font-bold">{lineage.metric_name}</code>
            </div>

            <div
              className="p-5 rounded-[32px] backdrop-blur-xl"
              style={{ background: 'rgba(255,255,255,0.65)', boxShadow: CLAY_SHADOW }}
            >
              <span className="text-xs text-[#4A7B76] font-bold uppercase tracking-wide block mb-2" style={{ fontFamily: 'DM Sans, sans-serif' }}>Source Model</span>
              <code className="text-sm text-[#4A7B76] font-mono">{lineage.source_model}</code>
            </div>

            <div
              className="p-5 rounded-[32px]"
              style={{ background: 'linear-gradient(135deg, #FCD34D, #F59E0B)', boxShadow: CLAY_SHADOW_PRIMARY }}
            >
              <span className="text-xs text-amber-900/80 font-bold uppercase tracking-wide block mb-2" style={{ fontFamily: 'DM Sans, sans-serif' }}>Upstream Models</span>
              <span className="text-2xl font-black text-white" style={{ fontFamily: 'Nunito, sans-serif' }}>{lineage.upstream_models?.length ?? 0}</span>
            </div>
          </div>

          {/* Lineage visual */}
          <div
            className="p-6 rounded-[32px] backdrop-blur-xl"
            style={{ background: 'rgba(255,255,255,0.5)', boxShadow: CLAY_INSET }}
          >
            <h3
              className="text-base font-bold text-[#1A3A38] mb-4"
              style={{ fontFamily: 'Nunito, sans-serif' }}
            >
              Transformation Path
            </h3>
            <LineageGraph
              path={lineagePath ?? lineage.lineage_path ?? []}
              steps={steps}
            />
          </div>

          {/* Steps table */}
          {steps.length > 0 && (
            <div
              className="rounded-[32px] overflow-hidden backdrop-blur-xl"
              style={{ background: 'rgba(255,255,255,0.65)', boxShadow: CLAY_SHADOW }}
            >
              <div
                className="px-6 py-4"
                style={{ borderBottom: '1px solid rgba(13,148,136,0.08)', background: 'rgba(255,255,255,0.40)' }}
              >
                <h3
                  className="text-base font-bold text-[#1A3A38]"
                  style={{ fontFamily: 'Nunito, sans-serif' }}
                >
                  Transformation Steps
                </h3>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm" style={{ fontFamily: 'DM Sans, sans-serif' }}>
                  <thead>
                    <tr style={{ background: '#0D9488' }}>
                      <th className="px-5 py-4 text-left text-xs font-bold text-white uppercase tracking-wider">Step</th>
                      <th className="px-5 py-4 text-left text-xs font-bold text-white uppercase tracking-wider">Model Name</th>
                      <th className="px-5 py-4 text-left text-xs font-bold text-white uppercase tracking-wider">Layer</th>
                      <th className="px-5 py-4 text-left text-xs font-bold text-white uppercase tracking-wider">Description</th>
                    </tr>
                  </thead>
                  <tbody>
                    {steps.map((s, i) => (
                      <tr
                        key={i}
                        className="hover:bg-[#F0FAF9]/60 transition-colors"
                        style={{ borderBottom: '1px solid rgba(13,148,136,0.06)' }}
                      >
                        <td className="px-5 py-3 text-[#4A7B76] font-mono text-xs">{i + 1}</td>
                        <td className="px-5 py-3">
                          <code className="text-xs font-mono text-[#1A3A38]">{s.model_name}</code>
                        </td>
                        <td className="px-5 py-3">
                          <StatusBadge status="info" label={s.layer ?? inferLayer(s.model_name, false)} />
                        </td>
                        <td className="px-5 py-3 text-[#4A7B76] text-xs max-w-xs">
                          {s.description || <span className="text-[#4A7B76]/40">—</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Source tables */}
          {lineage.source_tables?.length > 0 && (
            <div
              className="p-6 rounded-[32px] backdrop-blur-xl"
              style={{ background: 'rgba(255,255,255,0.65)', boxShadow: CLAY_SHADOW }}
            >
              <h3
                className="text-base font-bold text-[#1A3A38] mb-4"
                style={{ fontFamily: 'Nunito, sans-serif' }}
              >
                <GitBranch size={14} className="inline mr-2 text-[#0D9488]" />
                Source Tables
              </h3>
              <div className="flex flex-wrap gap-2">
                {lineage.source_tables.map((t) => (
                  <code
                    key={t}
                    className="px-4 py-1.5 rounded-[20px] text-xs text-[#1A3A38] font-mono backdrop-blur-xl"
                    style={{ background: 'rgba(255,255,255,0.80)', boxShadow: CLAY_SHADOW }}
                  >
                    {t}
                  </code>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {!selected && !lineageLoading && (
        <div
          className="text-center py-20 text-[#4A7B76] text-sm rounded-[32px] backdrop-blur-xl"
          style={{ background: 'rgba(255,255,255,0.40)', boxShadow: CLAY_SHADOW, fontFamily: 'DM Sans, sans-serif' }}
        >
          <GitBranch size={32} className="mx-auto mb-4 text-[#4A7B76]/40" />
          Select a metric above to explore its lineage graph.
        </div>
      )}
    </div>
  );
}
