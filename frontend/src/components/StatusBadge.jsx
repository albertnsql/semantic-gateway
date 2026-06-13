/**
 * components/StatusBadge.jsx — clay pill badge.
 * status: 'certified' | 'success' | 'rejected' | 'warning' | 'info' | 'error' | 'simple' | 'ratio' | 'dry_run'
 */

const VARIANTS = {
  certified: { color: '#10B981', bg: 'rgba(16,185,129,0.10)' },
  success:   { color: '#10B981', bg: 'rgba(16,185,129,0.10)' },
  rejected:  { color: '#F43F5E', bg: 'rgba(244,63,94,0.10)'  },
  error:     { color: '#F43F5E', bg: 'rgba(244,63,94,0.10)'  },
  warning:   { color: '#F59E0B', bg: 'rgba(245,158,11,0.10)' },
  info:      { color: '#0D9488', bg: 'rgba(13,148,136,0.10)' },
  simple:    { color: '#0D9488', bg: 'rgba(13,148,136,0.10)' },
  dry_run:   { color: '#0891B2', bg: 'rgba(8,145,178,0.10)'  },
  ratio:     { color: '#0891B2', bg: 'rgba(8,145,178,0.10)'  },
};

export default function StatusBadge({ status, label }) {
  const v    = VARIANTS[status?.toLowerCase()] ?? VARIANTS.info;
  const text = label ?? status ?? 'unknown';
  return (
    <span
      className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-bold"
      style={{
        color: v.color,
        background: v.bg,
        boxShadow: 'inset 2px 2px 4px rgba(255,255,255,0.6), inset -2px -2px 4px rgba(0,0,0,0.04)',
        fontFamily: 'DM Sans, sans-serif',
      }}
    >
      {text}
    </span>
  );
}
