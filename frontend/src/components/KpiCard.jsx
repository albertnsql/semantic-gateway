/**
 * components/KpiCard.jsx — clay-theme stat card used in QueryResultPanel.
 */

const CLAY_SHADOW = `16px 16px 32px rgba(13,148,136,0.12), -10px -10px 24px rgba(255,255,255,0.9), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;

export default function KpiCard({ label, value, subLabel, icon: Icon, accent = false }) {
  return (
    <div
      className="p-5 flex flex-col gap-1 rounded-[32px] backdrop-blur-xl"
      style={{
        background: 'rgba(255,255,255,0.65)',
        boxShadow: CLAY_SHADOW,
        borderLeft: `4px solid ${accent ? '#0D9488' : 'rgba(13,148,136,0.25)'}`,
      }}
    >
      <div className="flex items-center justify-between mb-1">
        <span
          className="text-[10px] font-bold text-[#4A7B76] uppercase tracking-widest"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          {label}
        </span>
        {Icon && <Icon size={15} className="text-[#4A7B76]/40" />}
      </div>
      <span
        className="text-2xl font-black text-[#0D9488]"
        style={{ fontFamily: 'Nunito, sans-serif' }}
      >
        {value ?? '—'}
      </span>
      {subLabel && (
        <span
          className="text-xs text-[#4A7B76]"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          {subLabel}
        </span>
      )}
    </div>
  );
}
