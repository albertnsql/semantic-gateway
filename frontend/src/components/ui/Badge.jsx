import React from 'react';

const CLAY_SHADOW = `16px 16px 32px rgba(13,148,136,0.12), -10px -10px 24px rgba(255,255,255,0.9), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;

export function Badge({ children, variant = 'teal', className = '' }) {
  const variants = {
    teal:    { color: '#0D9488', bg: 'rgba(13,148,136,0.10)' },
    emerald: { color: '#10B981', bg: 'rgba(16,185,129,0.10)' },
    amber:   { color: '#F59E0B', bg: 'rgba(245,158,11,0.10)' },
    red:     { color: '#F43F5E', bg: 'rgba(244,63,94,0.10)' },
    blue:    { color: '#0891B2', bg: 'rgba(8,145,178,0.10)' },
    gray:    { color: '#4A7B76', bg: 'rgba(74,123,118,0.08)' },
  };

  const s = variants[variant] || variants.teal;

  return (
    <span
      className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold ${className}`}
      style={{
        color: s.color,
        background: s.bg,
        boxShadow: 'inset 2px 2px 4px rgba(255,255,255,0.6), inset -2px -2px 4px rgba(0,0,0,0.04)',
        fontFamily: 'DM Sans, sans-serif',
      }}
    >
      {children}
    </span>
  );
}
