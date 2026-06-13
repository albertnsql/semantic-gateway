import React from 'react';

const CLAY_SHADOW = `
  16px 16px 32px rgba(13, 148, 136, 0.12),
  -10px -10px 24px rgba(255, 255, 255, 0.9),
  inset 6px 6px 12px rgba(13, 148, 136, 0.04),
  inset -6px -6px 12px rgba(255, 255, 255, 1)
`;
const CLAY_SHADOW_HOVER = `
  20px 20px 40px rgba(13, 148, 136, 0.18),
  -12px -12px 28px rgba(255, 255, 255, 0.95),
  inset 6px 6px 12px rgba(13, 148, 136, 0.04),
  inset -6px -6px 12px rgba(255, 255, 255, 1)
`;

export function Card({ children, className = '', hover = false, style = {}, ...props }) {
  return (
    <div
      className={`rounded-[32px] backdrop-blur-xl transition-all duration-500 p-4
        ${hover ? 'cursor-pointer hover:-translate-y-2' : ''}
        ${className}`}
      style={{
        background: 'rgba(255,255,255,0.65)',
        boxShadow: CLAY_SHADOW,
        ...style,
      }}
      onMouseEnter={hover ? (e) => { e.currentTarget.style.boxShadow = CLAY_SHADOW_HOVER; } : undefined}
      onMouseLeave={hover ? (e) => { e.currentTarget.style.boxShadow = CLAY_SHADOW; } : undefined}
      {...props}
    >
      {children}
    </div>
  );
}

export function CardHeader({ title, subtitle, action, className = '' }) {
  return (
    <div className={`flex items-start justify-between mb-4 ${className}`}>
      <div>
        <h3
          className="text-sm font-bold text-[#1A3A38]"
          style={{ fontFamily: 'Nunito, sans-serif' }}
        >
          {title}
        </h3>
        {subtitle && (
          <p className="text-xs text-[#4A7B76] mt-0.5" style={{ fontFamily: 'DM Sans, sans-serif' }}>
            {subtitle}
          </p>
        )}
      </div>
      {action && <div>{action}</div>}
    </div>
  );
}
