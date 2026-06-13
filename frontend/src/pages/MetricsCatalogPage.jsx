/**
 * pages/MetricsCatalogPage.jsx — Claymorphism certified metrics browser.
 */
import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { BarChart3, ChevronDown, ChevronUp, ArrowRight, ShieldCheck } from 'lucide-react';
import TopBar from '../components/TopBar';
import StatusBadge from '../components/StatusBadge';
import LoadingSpinner from '../components/LoadingSpinner';
import ErrorState from '../components/ErrorState';
import { getMetrics } from '../api/metrics';

const CLAY_SHADOW = `16px 16px 32px rgba(13,148,136,0.12), -10px -10px 24px rgba(255,255,255,0.9), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;
const CLAY_SHADOW_HOVER = `20px 20px 40px rgba(13,148,136,0.18), -12px -12px 28px rgba(255,255,255,0.95), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;

function MetricCard({ metric }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div
      className="flex flex-col gap-0 cursor-pointer rounded-[32px] overflow-hidden backdrop-blur-xl
                 transition-all duration-500 hover:-translate-y-2"
      style={{ background: 'rgba(255,255,255,0.65)', boxShadow: expanded ? CLAY_SHADOW_HOVER : CLAY_SHADOW }}
      onMouseEnter={(e) => { e.currentTarget.style.boxShadow = CLAY_SHADOW_HOVER; }}
      onMouseLeave={(e) => { e.currentTarget.style.boxShadow = expanded ? CLAY_SHADOW_HOVER : CLAY_SHADOW; }}
      onClick={() => setExpanded((v) => !v)}
    >
      {/* Card header */}
      <div className="flex items-start justify-between p-6">
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <span
              className="font-mono text-[#0D9488] font-bold text-sm"
              style={{ fontFamily: 'JetBrains Mono, monospace' }}
            >
              {metric.name}
            </span>
            <StatusBadge status="certified" label="Certified" />
          </div>
          <span
            className="text-[#1A3A38] text-lg font-black tracking-tight"
            style={{ fontFamily: 'Nunito, sans-serif' }}
          >
            {metric.label}
          </span>
        </div>
        {expanded
          ? <ChevronUp size={16} className="text-[#4A7B76] shrink-0 mt-1" />
          : <ChevronDown size={16} className="text-[#4A7B76] shrink-0 mt-1" />
        }
      </div>

      {/* Always visible rows */}
      <div
        className="px-6 pb-5 flex flex-col gap-3"
        style={{ borderTop: '1px solid rgba(13,148,136,0.08)', paddingTop: '1rem' }}
      >
        <div className="flex gap-3 flex-wrap">
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-[#1A3A38] font-bold" style={{ fontFamily: 'DM Sans, sans-serif' }}>Type:</span>
            <StatusBadge status={metric.metric_type} label={metric.metric_type} />
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-[#1A3A38] font-bold" style={{ fontFamily: 'DM Sans, sans-serif' }}>Source:</span>
            <code className="text-xs text-[#0E7490] font-bold font-mono">{metric.source_model}</code>
          </div>
        </div>

        <div className="flex flex-col gap-1">
          <span className="text-xs text-[#1A3A38] font-bold" style={{ fontFamily: 'DM Sans, sans-serif' }}>Grain:</span>
          <code className="text-xs text-[#115E59] font-bold font-mono leading-relaxed">{metric.grain}</code>
        </div>

        {metric.certified_dimensions?.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <span className="text-xs text-[#1A3A38] font-bold" style={{ fontFamily: 'DM Sans, sans-serif' }}>
              Dimensions ({metric.certified_dimensions.length}):
            </span>
            <div className="flex flex-wrap gap-1">
              {metric.certified_dimensions.slice(0, expanded ? 999 : 6).map((d) => (
                <span
                  key={d}
                  className="px-2 py-0.5 text-[10px] font-mono font-bold rounded-full text-[#115E59]"
                  style={{
                    background: 'rgba(13,148,136,0.08)',
                    boxShadow: 'inset 2px 2px 4px rgba(13,148,136,0.08), inset -2px -2px 4px rgba(255,255,255,0.9)',
                  }}
                >
                  {d}
                </span>
              ))}
              {!expanded && metric.certified_dimensions.length > 6 && (
                <span
                  className="px-2 py-0.5 text-[10px] font-mono font-bold rounded-full text-[#1A3A38]"
                  style={{
                    background: 'rgba(74,123,118,0.08)',
                    boxShadow: 'inset 2px 2px 4px rgba(74,123,118,0.06), inset -2px -2px 4px rgba(255,255,255,0.9)',
                  }}
                >
                  +{metric.certified_dimensions.length - 6} more
                </span>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Expanded section */}
      {expanded && (
        <div
          className="px-6 pb-6 flex flex-col gap-4 animate-slide-in"
          style={{ borderTop: '1px solid rgba(13,148,136,0.08)', paddingTop: '1.25rem' }}
          onClick={(e) => e.stopPropagation()}
        >
          {metric.description && (
            <div>
              <span className="text-xs text-[#1A3A38] font-black uppercase tracking-wide block mb-1" style={{ fontFamily: 'DM Sans, sans-serif' }}>Description</span>
              <p className="text-[#1A3A38] text-sm leading-relaxed" style={{ fontFamily: 'DM Sans, sans-serif' }}>{metric.description}</p>
            </div>
          )}
          {metric.certified_definition && (
            <div>
              <span className="text-xs text-[#1A3A38] font-black uppercase tracking-wide block mb-1" style={{ fontFamily: 'DM Sans, sans-serif' }}>Certified Definition</span>
              <p className="text-[#1A3A38] text-sm leading-relaxed" style={{ fontFamily: 'DM Sans, sans-serif' }}>{metric.certified_definition}</p>
            </div>
          )}
          {metric.grain_columns?.length > 0 && (
            <div>
              <span className="text-xs text-[#1A3A38] font-black uppercase tracking-wide block mb-1.5" style={{ fontFamily: 'DM Sans, sans-serif' }}>Grain Columns</span>
              <div className="flex flex-wrap gap-1.5">
                {metric.grain_columns.map((c) => (
                  <code
                    key={c}
                    className="px-3 py-0.5 rounded-full text-xs text-[#1A3A38] font-mono"
                    style={{
                      background: 'rgba(255,255,255,0.80)',
                      boxShadow: CLAY_SHADOW,
                    }}
                  >
                    {c}
                  </code>
                ))}
              </div>
            </div>
          )}

          <Link
            to={`/lineage?metric=${metric.name}`}
            className="inline-flex items-center gap-1.5 text-[#0D9488] text-sm font-bold
                       hover:text-[#0D9488]/80 transition-colors group mt-1 w-fit"
            style={{ fontFamily: 'DM Sans, sans-serif' }}
            onClick={(e) => e.stopPropagation()}
          >
            View Lineage <ArrowRight size={14} className="group-hover:translate-x-0.5 transition-transform" />
          </Link>
        </div>
      )}
    </div>
  );
}

export default function MetricsCatalogPage() {
  const [metrics, setMetrics] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState(null);

  useEffect(() => {
    getMetrics()
      .then(setMetrics)
      .catch(setError)
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="max-w-5xl mx-auto flex flex-col gap-6 animate-fade-in">
      <TopBar title="Certified Metrics" breadcrumb={['Gateway', 'Metrics']} />

      <div className="flex items-center gap-3 mb-2">
        <div
          className="w-8 h-8 rounded-full flex items-center justify-center bg-gradient-to-br from-emerald-400 to-emerald-600"
          style={{ boxShadow: '8px 8px 16px rgba(16,185,129,0.20), -6px -6px 12px rgba(255,255,255,0.9)' }}
        >
          <ShieldCheck size={15} className="text-white" />
        </div>
        <p className="text-[#4A7B76] text-sm font-medium" style={{ fontFamily: 'DM Sans, sans-serif' }}>
          {loading ? 'Loading…' : `${metrics.length} MetricFlow-certified metrics powering the gateway`}
        </p>
      </div>

      {loading && <LoadingSpinner label="Fetching certified metrics…" />}
      {error && <ErrorState message="Failed to load metrics catalog" detail={error.message} />}
      {!loading && !error && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
          {metrics.map((m) => (
            <MetricCard key={m.name} metric={m} />
          ))}
        </div>
      )}
    </div>
  );
}
