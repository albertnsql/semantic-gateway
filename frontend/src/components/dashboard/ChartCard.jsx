import React from 'react';

const CLAY_SHADOW = `16px 16px 32px rgba(13,148,136,0.12), -10px -10px 24px rgba(255,255,255,0.9), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;

export default function ChartCard({ title, badge, isMock, children }) {
  return (
    <div
      className="flex flex-col h-full rounded-[32px] overflow-hidden relative backdrop-blur-xl"
      style={{ background: 'rgba(255,255,255,0.65)', boxShadow: CLAY_SHADOW }}
    >
      {/* Header */}
      <div
        className="flex justify-between items-center px-5 pt-4 pb-3"
        style={{
          borderBottom: '1px solid rgba(13,148,136,0.08)',
          background: 'rgba(255,255,255,0.40)',
        }}
      >
        <h3
          className="text-sm font-bold text-[#1A3A38] tracking-tight"
          style={{ fontFamily: 'Nunito, sans-serif' }}
        >
          {title}
        </h3>

        {isMock ? (
          <div className="relative group">
            <span
              className="inline-flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest
                         text-[#F59E0B] px-2.5 py-1 rounded-full cursor-help whitespace-nowrap shrink-0"
              style={{
                background: 'rgba(245,158,11,0.10)',
                boxShadow: 'inset 2px 2px 4px rgba(245,158,11,0.10), inset -2px -2px 4px rgba(255,255,255,0.9)',
              }}
            >
              <div className="w-1.5 h-1.5 rounded-full bg-[#F59E0B]" />
              Estimated
            </span>
            <div
              className="absolute hidden group-hover:block top-full right-0 mt-2 w-56 p-3 rounded-[20px]
                         text-xs text-[#4A7B76] z-20 backdrop-blur-xl"
              style={{ background: 'rgba(255,255,255,0.95)', boxShadow: CLAY_SHADOW, fontFamily: 'DM Sans, sans-serif' }}
            >
              Live data unavailable — showing estimated values
            </div>
          </div>
        ) : badge ? (
          <span
            className="inline-flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest
                       text-slate-500 px-2.5 py-1 rounded-full whitespace-nowrap shrink-0"
            style={{
              background: 'rgba(148,163,184,0.10)',
              boxShadow: 'inset 2px 2px 4px rgba(148,163,184,0.08), inset -2px -2px 4px rgba(255,255,255,0.9)',
            }}
          >
            {badge}
          </span>
        ) : null}
      </div>

      {/* Chart content */}
      <div className="flex-1 w-full min-h-0 p-4">
        {children}
      </div>
    </div>
  );
}
