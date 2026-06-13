import React, { useState } from 'react';
import { X, Database, AlertTriangle, Info, Lightbulb, ChevronRight } from 'lucide-react';
import SqlViewer from '../SqlViewer';

export default function ChatMessage({ message, onSuggest, onSuggestPopulate }) {
  const [showAllRows, setShowAllRows] = useState(false);
  const isUser = message.role === 'user';

  if (isUser) {
    return (
      <div
        className="self-end max-w-[85%] px-4 py-2.5 rounded-[20px] rounded-br-[6px]"
        style={{ background: 'linear-gradient(135deg, #2DD4BF, #0D9488)', boxShadow: '8px 8px 16px rgba(13,148,136,0.20), -4px -4px 8px rgba(255,255,255,0.3)' }}
      >
        <p className="text-sm leading-relaxed text-white" style={{ fontFamily: 'DM Sans, sans-serif' }}>{message.content}</p>
        {message.date && (
          <p className="text-[9px] text-white/60 mt-1 text-right">
            {message.date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </p>
        )}
      </div>
    );
  }

  // Error
  if (message.status === 'error') {
    return (
      <div
        className="self-start max-w-[85%] px-4 py-3 rounded-[20px] rounded-tl-[6px] backdrop-blur-xl"
        style={{ background: 'rgba(255,255,255,0.80)', boxShadow: '8px 8px 20px rgba(13,148,136,0.08), -6px -6px 16px rgba(255,255,255,0.9)' }}
      >
        <div className="flex items-center gap-1.5 text-[#F59E0B] font-bold text-xs mb-1.5">
          <AlertTriangle size={13} /> Query Error
        </div>
        <p className="text-sm text-[#1A3A38]" style={{ fontFamily: 'DM Sans, sans-serif' }}>{message.error}</p>
        {message.date && (
          <p className="text-[9px] text-[#4A7B76] mt-1">
            {message.date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </p>
        )}
      </div>
    );
  }

  // Rejected
  if (message.status === 'rejected') {
    const raw = message.raw;
    return (
      <div
        className="self-start max-w-[95%] w-full px-4 py-3 rounded-[20px] rounded-tl-[6px] backdrop-blur-xl"
        style={{ background: 'rgba(255,255,255,0.80)', boxShadow: '8px 8px 20px rgba(244,63,94,0.06), -6px -6px 16px rgba(255,255,255,0.9)', borderTop: '2px solid #F43F5E' }}
      >
        <div className="text-[#F43F5E] font-bold text-xs mb-2 flex items-center gap-1.5" style={{ fontFamily: 'DM Sans, sans-serif' }}>
          <X size={13} /> Query Rejected: {raw?.rejection?.reason || 'Governance rule violated'}
        </div>
        {raw?.validation?.violations?.length > 0 && (
          <div className="flex flex-wrap gap-1 mb-2">
            {raw.validation.violations.map((v, i) => (
              <span
                key={i}
                className="px-2 py-0.5 rounded-full text-[#F43F5E] text-[10px]"
                style={{ background: 'rgba(244,63,94,0.10)', boxShadow: 'inset 2px 2px 4px rgba(244,63,94,0.06), inset -2px -2px 4px rgba(255,255,255,0.9)' }}
              >
                {v.message}
              </span>
            ))}
          </div>
        )}
        {raw?.rejection?.suggested_fix && (
          <p className="text-xs text-[#F59E0B] mb-2 font-medium" style={{ fontFamily: 'DM Sans, sans-serif' }}>Suggested fix: {raw.rejection.suggested_fix}</p>
        )}
        {raw?.rejection?.safe_alternatives?.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {raw.rejection.safe_alternatives.map((alt, i) => (
              <button
                key={i}
                onClick={() => onSuggest(alt)}
                className="px-2.5 py-1 rounded-full text-[#0D9488] text-[10px] font-bold transition-all duration-200 hover:-translate-y-0.5"
                style={{ background: 'rgba(13,148,136,0.08)', boxShadow: '4px 4px 8px rgba(13,148,136,0.08), -3px -3px 6px rgba(255,255,255,0.9)' }}
              >
                {alt}
              </button>
            ))}
          </div>
        )}
      </div>
    );
  }

  // Needs Clarification
  if (message.status === 'needs_clarification') {
    const raw = message.raw;
    return (
      <div
        className="self-start max-w-[95%] w-full px-4 py-3 rounded-[20px] rounded-tl-[6px] backdrop-blur-xl"
        style={{ background: 'rgba(255,255,255,0.80)', boxShadow: '8px 8px 20px rgba(13,148,136,0.08), -6px -6px 16px rgba(255,255,255,0.9)' }}
      >
        <div className="text-[#0D9488] font-bold text-xs mb-2 flex items-center gap-1.5" style={{ fontFamily: 'DM Sans, sans-serif' }}>
           Clarification Needed
        </div>
        <p className="text-sm text-[#1A3A38] leading-relaxed mb-2" style={{ fontFamily: 'DM Sans, sans-serif' }}>{raw?.message}</p>
        
        {(raw?.available_options?.time_grains?.length > 0 || raw?.available_options?.dimensions?.length > 0) && (
          <div className="flex flex-col gap-2 mt-2">
            {raw.available_options?.time_grains?.length > 0 && (
              <div>
                <span className="text-[10px] text-[#4A7B76] font-bold uppercase tracking-wide block mb-1">Available Time Granularities</span>
                <div className="flex flex-wrap gap-1">
                  {raw.available_options.time_grains.map(g => (
                    <button key={g} onClick={() => onSuggest(g)} className="px-2 py-0.5 rounded-full text-xs text-[#0D9488] bg-[#0D9488]/10 font-mono hover:bg-[#0D9488]/20 transition-colors">{g}</button>
                  ))}
                </div>
              </div>
            )}
            {raw.available_options?.dimensions?.length > 0 && (
              <div>
                <span className="text-[10px] text-[#4A7B76] font-bold uppercase tracking-wide block mb-1">Available Dimensions</span>
                <div className="flex flex-wrap gap-1">
                  {raw.available_options.dimensions.map(d => (
                    <button key={d} onClick={() => onSuggest(d)} className="px-2 py-0.5 rounded-full text-xs text-[#0D9488] bg-[#0D9488]/10 font-mono hover:bg-[#0D9488]/20 transition-colors">{d}</button>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  // Schema response
  if (message.status === 'schema_response') {
    const raw = message.raw;
    return (
      <div
        className="self-start max-w-[95%] w-full px-4 py-3 rounded-[20px] rounded-tl-[6px] backdrop-blur-xl border-l-4 border-[#0D9488]"
        style={{ background: 'rgba(255,255,255,0.80)', boxShadow: '8px 8px 20px rgba(13,148,136,0.08), -6px -6px 16px rgba(255,255,255,0.9)' }}
      >
        <div className="flex items-center gap-1.5 text-[#0D9488] font-bold text-xs mb-2" style={{ fontFamily: 'DM Sans, sans-serif' }}>
          <Info size={13} /> What's available
        </div>
        <p className="text-sm text-[#1A3A38] leading-relaxed whitespace-pre-line" style={{ fontFamily: 'DM Sans, sans-serif' }}>
          {raw?.message || message.content}
        </p>
        {message.date && (
          <p className="text-[9px] text-[#4A7B76] mt-2">
            {message.date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </p>
        )}
      </div>
    );
  }

  // Out of scope
  if (message.status === 'out_of_scope') {
    const raw = message.raw;
    const suggestedQuery = raw?.suggested_query;
    return (
      <div
        className="self-start max-w-[95%] w-full px-4 py-3 rounded-[20px] rounded-tl-[6px] backdrop-blur-xl border-l-4 border-[#F59E0B]"
        style={{ background: 'rgba(255,255,255,0.80)', boxShadow: '8px 8px 20px rgba(245,158,11,0.06), -6px -6px 16px rgba(255,255,255,0.9)' }}
      >
        <div className="flex items-center gap-1.5 text-[#F59E0B] font-bold text-xs mb-2" style={{ fontFamily: 'DM Sans, sans-serif' }}>
          <Lightbulb size={13} /> Try a data question
        </div>
        <p className="text-sm text-[#1A3A38] leading-relaxed mb-2" style={{ fontFamily: 'DM Sans, sans-serif' }}>
          {raw?.message || message.content}
        </p>
        {suggestedQuery && (
          <button
            onClick={() => onSuggestPopulate && onSuggestPopulate(suggestedQuery)}
            className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full text-[#0D9488] text-[10px] font-bold transition-all duration-200 hover:-translate-y-0.5"
            style={{ background: 'rgba(13,148,136,0.08)', boxShadow: '4px 4px 8px rgba(13,148,136,0.08), -3px -3px 6px rgba(255,255,255,0.9)' }}
            title="Populate input (does not auto-submit)"
          >
            <ChevronRight size={11} />
            {suggestedQuery}
          </button>
        )}
        {message.date && (
          <p className="text-[9px] text-[#4A7B76] mt-2">
            {message.date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </p>
        )}
      </div>
    );
  }

  // Success / welcome
  const raw = message.raw;
  return (
    <div
      className="self-start max-w-[95%] w-full px-4 py-3 rounded-[20px] rounded-tl-[6px] backdrop-blur-xl"
      style={{ background: 'rgba(255,255,255,0.80)', boxShadow: '8px 8px 20px rgba(13,148,136,0.08), -6px -6px 16px rgba(255,255,255,0.9)' }}
    >
      {(raw?.narrative_summary || message.content) && (
        <p className="text-sm text-[#1A3A38] leading-relaxed mb-3 whitespace-pre-line" style={{ fontFamily: 'DM Sans, sans-serif' }}>
          {raw?.narrative_summary || message.content}
        </p>
      )}
      {raw && (
        <div className="flex flex-col gap-2">
          {(raw.query?.interpreted_metrics?.length > 0 || raw.query?.interpreted_dimensions?.length > 0) && (
            <div className="flex flex-wrap gap-1">
              {raw.query?.interpreted_metrics?.map(m => (
                <span
                  key={m}
                  className="px-2 py-0.5 rounded-full text-[#0D9488] text-[10px] font-medium"
                  style={{ background: 'rgba(13,148,136,0.10)', boxShadow: 'inset 2px 2px 4px rgba(13,148,136,0.08), inset -2px -2px 4px rgba(255,255,255,0.9)' }}
                >
                  Metric: {m}
                </span>
              ))}
              {raw.query?.interpreted_dimensions?.map(d => (
                <span
                  key={d}
                  className="px-2 py-0.5 rounded-full text-[#0891B2] text-[10px] font-medium"
                  style={{ background: 'rgba(8,145,178,0.10)', boxShadow: 'inset 2px 2px 4px rgba(8,145,178,0.08), inset -2px -2px 4px rgba(255,255,255,0.9)' }}
                >
                  Dim: {d}
                </span>
              ))}
            </div>
          )}

          {raw.result?.data?.length > 0 && (
            <details className="group mt-1">
              <summary className="text-[11px] text-[#4A7B76] hover:text-[#0D9488] cursor-pointer select-none flex items-center gap-1 font-medium list-none mb-2">
                <span className="group-open:hidden">▸</span>
                <span className="hidden group-open:inline">▾</span>
                View Data Table
              </summary>
              <div className="rounded-[20px] overflow-hidden" style={{ boxShadow: 'inset 4px 4px 8px rgba(13,148,136,0.06), inset -4px -4px 8px rgba(255,255,255,0.9)' }}>
                <table className="w-full text-left border-collapse">
                  <thead>
                    <tr style={{ background: 'rgba(13,148,136,0.06)', borderBottom: '1px solid rgba(13,148,136,0.10)' }}>
                      {Object.keys(raw.result.data[0]).map(k => (
                        <th key={k} className="px-2.5 py-1.5 text-[10px] font-bold text-[#4A7B76] uppercase tracking-wide" style={{ borderBottom: '1px solid rgba(13,148,136,0.10)' }}>
                          {k}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {raw.result.data.slice(0, showAllRows ? raw.result.data.length : 4).map((row, i) => (
                      <tr key={i} className="hover:bg-[#F0FAF9]/60 transition-colors" style={{ borderBottom: '1px solid rgba(13,148,136,0.06)' }}>
                        {Object.values(row).map((v, j) => (
                          <td key={j} className="px-2.5 py-1.5 text-[#1A3A38] text-xs">
                            {typeof v === 'number' ? v.toLocaleString() : String(v)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
                {raw.result.data.length > 4 && !showAllRows && (
                  <button
                    onClick={() => setShowAllRows(true)}
                    className="w-full text-left px-2.5 py-1.5 text-[10px] text-[#0D9488] hover:bg-[#14B8A6]/10 cursor-pointer transition-colors"
                    style={{ background: 'rgba(13,148,136,0.04)', borderTop: '1px solid rgba(13,148,136,0.08)' }}
                  >
                    + {raw.result.data.length - 4} more rows
                  </button>
                )}
                {raw.result.data.length > 4 && showAllRows && (
                  <button
                    onClick={() => setShowAllRows(false)}
                    className="w-full text-left px-2.5 py-1.5 text-[10px] text-[#0D9488] hover:bg-[#14B8A6]/10 cursor-pointer transition-colors"
                    style={{ background: 'rgba(13,148,136,0.04)', borderTop: '1px solid rgba(13,148,136,0.08)' }}
                  >
                    - Show less
                  </button>
                )}
              </div>
            </details>
          )}

          <div className="flex items-center justify-between text-[10px] text-[#4A7B76] font-mono">
            <span>Grain: {raw.governance?.grain || 'N/A'}</span>
            {raw.governance?.lineage_path?.length > 0 && <span>raw → stg → fct → metric</span>}
          </div>

          {raw.governance?.warning && (
            <div
              className="flex items-start gap-1.5 text-xs text-[#F59E0B] px-3 py-2 rounded-[16px]"
              style={{ background: 'rgba(245,158,11,0.08)', boxShadow: 'inset 2px 2px 4px rgba(245,158,11,0.06), inset -2px -2px 4px rgba(255,255,255,0.9)' }}
            >
              <AlertTriangle size={12} className="mt-0.5 flex-shrink-0" />
              {raw.governance.warning}
            </div>
          )}

          {raw.result?.generated_sql && (
            <details className="group">
              <summary className="text-[11px] text-[#4A7B76] hover:text-[#0D9488] cursor-pointer select-none flex items-center gap-1 font-medium list-none">
                <span className="group-open:hidden">▸</span>
                <span className="hidden group-open:inline">▾</span>
                View SQL
              </summary>
              <div
                className="mt-2 rounded-[20px] overflow-hidden max-h-36 overflow-y-auto"
                style={{ borderLeft: '2px solid #0D9488', background: 'rgba(13,148,136,0.04)' }}
              >
                <SqlViewer sql={raw.result.generated_sql} />
              </div>
            </details>
          )}
        </div>
      )}
      {message.date && (
        <p className="text-[9px] text-[#4A7B76] mt-2">
          {message.date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
        </p>
      )}
    </div>
  );
}
