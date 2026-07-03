import React from 'react';
import { TrendingUp, TrendingDown } from 'lucide-react';

const CLAY_SHADOW = `16px 16px 32px rgba(13,148,136,0.12), -10px -10px 24px rgba(255,255,255,0.9), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;
const CLAY_SHADOW_HOVER = `20px 20px 40px rgba(13,148,136,0.18), -12px -12px 28px rgba(255,255,255,0.95), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;

export default function KpiTile({
  label,
  value,
  prevValue,
  formatter,
  trend,
  trendIsGood,
  loading,
  error
}) {
  if (loading) {
    return (
      <div
        className="rounded-[32px] p-6 flex flex-col justify-center gap-3 h-[140px] backdrop-blur-xl"
        style={{ background: 'rgba(255,255,255,0.65)', boxShadow: CLAY_SHADOW }}
      >
        <div className="w-1/2 h-4 rounded-[20px] animate-pulse" style={{ background: 'rgba(13,148,136,0.10)' }} />
        <div className="w-3/4 h-8 rounded-[20px] animate-pulse mt-1" style={{ background: 'rgba(13,148,136,0.08)' }} />
        <div className="w-1/3 h-3 rounded-[20px] animate-pulse" style={{ background: 'rgba(13,148,136,0.06)' }} />
      </div>
    );
  }

  if (error) {
    return (
      <div
        className="rounded-[32px] p-6 flex flex-col justify-between group relative overflow-hidden h-[140px] backdrop-blur-xl"
        style={{ background: 'rgba(255,255,255,0.65)', boxShadow: CLAY_SHADOW }}
      >
        <span className="text-xs font-bold text-[#4A7B76] uppercase tracking-widest" style={{ fontFamily: 'DM Sans, sans-serif' }}>{label}</span>
        <div className="flex items-center gap-2">
          <span className="text-3xl font-black text-[#1A3A38]" style={{ fontFamily: 'Nunito, sans-serif' }}>—</span>
          <div className="w-2 h-2 rounded-full bg-[#F43F5E] cursor-help flex-shrink-0" title={error} />
        </div>
        <span className="text-xs text-[#4A7B76]" style={{ fontFamily: 'DM Sans, sans-serif' }}>No data available</span>
        <div className="absolute hidden group-hover:block bottom-full left-0 mb-2 w-52 p-3 rounded-[20px] text-xs text-[#4A7B76] z-20 backdrop-blur-xl"
          style={{ background: 'rgba(255,255,255,0.90)', boxShadow: CLAY_SHADOW, fontFamily: 'DM Sans, sans-serif' }}>
          {error}
        </div>
      </div>
    );
  }

  const isPositive = trend > 0;
  const trendGreen = (isPositive && trendIsGood) || (!isPositive && !trendIsGood);
  const trendColor = trendGreen ? 'text-emerald-600' : 'text-[#F43F5E]';
  const trendBg    = trendGreen ? 'rgba(16,185,129,0.10)' : 'rgba(244,63,94,0.10)';
  const TrendIcon  = isPositive ? TrendingUp : TrendingDown;

  // Decorative sparkline
  const sparklineColor = trendGreen ? '#10B981' : '#F43F5E';
  const sparklinePoints = isPositive
    ? '0,20 10,18 20,22 30,12 40,15 50,5'
    : '0,5 10,8 20,4 30,15 40,12 50,20';

  return (
    <div
      className="rounded-[32px] p-6 flex flex-col justify-between relative overflow-hidden h-[140px] backdrop-blur-xl
                 cursor-pointer transition-all duration-500 hover:-translate-y-2 group"
      style={{ background: 'rgba(255,255,255,0.65)', boxShadow: CLAY_SHADOW }}
      onMouseEnter={(e) => { e.currentTarget.style.boxShadow = CLAY_SHADOW_HOVER; }}
      onMouseLeave={(e) => { e.currentTarget.style.boxShadow = CLAY_SHADOW; }}
    >
      {/* Top teal accent bar */}
      <div
        className="absolute inset-x-0 top-0 h-1 rounded-t-[32px]"
        style={{ background: 'linear-gradient(90deg, #2DD4BF, #0D9488, transparent)' }}
      />

      <div className="relative z-10">
        <h3
          className="text-xs font-bold text-[#4A7B76] uppercase tracking-widest truncate"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          {label}
        </h3>
        <div className="mt-1.5 flex items-baseline gap-2">
          <span
            className="text-[28px] font-black text-[#1A3A38] tracking-tight leading-none"
            style={{ fontFamily: 'Nunito, sans-serif' }}
          >
            {value}
          </span>
        </div>

        {trend != null ? (
          <div className="mt-2.5 flex items-center gap-1.5 relative group/trend">
            <div
              className={`flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-bold ${trendColor} cursor-default`}
              style={{ background: trendBg }}
            >
              <TrendIcon size={11} strokeWidth={2.5} />
              <span>{Math.abs(trend)}%</span>
            </div>
            <span className="text-xs text-[#4A7B76]" style={{ fontFamily: 'DM Sans, sans-serif' }}>vs last year</span>
            
            {/* Custom Tooltip for Previous Month Value */}
            {prevValue != null && (
              <div className="absolute hidden group-hover/trend:block bottom-full left-0 mb-2 w-max px-3 py-1.5 rounded-[12px] text-xs font-bold text-[#1A3A38] z-20 backdrop-blur-xl animate-fade-in"
                   style={{ background: 'rgba(255,255,255,0.95)', boxShadow: CLAY_SHADOW, fontFamily: 'DM Sans, sans-serif' }}>
                <span className="text-[#4A7B76] font-medium mr-1">Prior Year:</span>
                {formatter ? formatter(prevValue) : prevValue}
              </div>
            )}
          </div>
        ) : (
          <div className="mt-2.5 h-[20px]" />
        )}
      </div>

      {/* Decorative Sparkline */}
      <div className="absolute bottom-0 right-0 w-[80px] h-[40px] opacity-20 pointer-events-none group-hover:opacity-40 transition-opacity">
        <svg viewBox="0 0 50 30" className="w-full h-full" preserveAspectRatio="none">
          <polyline
            points={sparklinePoints}
            fill="none"
            stroke={sparklineColor}
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
    </div>
  );
}
