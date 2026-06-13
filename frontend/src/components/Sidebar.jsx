import React from 'react';
import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard, MessageSquare, BarChart3, GitBranch,
  Play, Terminal, PieChart,
} from 'lucide-react';

const NAV_ITEMS = [
  { path: '/',          label: 'Overview',         icon: LayoutDashboard },
  { path: '/dashboard', label: 'Dashboard',         icon: PieChart },
  { path: '/query',     label: 'Query Interface',   icon: MessageSquare, dot: true },
  { path: '/metrics',   label: 'Metrics Catalog',   icon: BarChart3 },
  { path: '/lineage',   label: 'Lineage Explorer',  icon: GitBranch },
  { path: '/demo',      label: 'Demo Scenarios',    icon: Play },
];

export default function Sidebar({ apiHealthy }) {
  return (
    <aside
      className="w-[220px] h-screen flex flex-col shrink-0 rounded-r-[40px] relative z-20"
      style={{
        background: 'rgba(255,255,255,0.70)',
        backdropFilter: 'blur(24px)',
        WebkitBackdropFilter: 'blur(24px)',
        boxShadow: `
          30px 30px 60px rgba(13, 148, 136, 0.08),
          -30px -30px 60px #ffffff,
          inset 10px 10px 20px rgba(13, 148, 136, 0.04),
          inset -10px -10px 20px rgba(255, 255, 255, 0.8)
        `,
      }}
    >
      {/* ── Logo area ── */}
      <div className="flex items-center gap-2 px-4 py-6">
        <div
          className="w-8 h-8 rounded-[16px] flex items-center justify-center shrink-0"
          style={{
            background: 'linear-gradient(135deg, #2DD4BF, #0D9488)',
            boxShadow: `
              12px 12px 24px rgba(13, 148, 136, 0.30),
              -8px -8px 16px rgba(255, 255, 255, 0.4),
              inset 4px 4px 8px rgba(255, 255, 255, 0.4),
              inset -4px -4px 8px rgba(0, 0, 0, 0.08)
            `,
          }}
        >
          <Terminal size={16} className="text-white" />
        </div>
        <span
          className="text-sm font-black tracking-tight text-[#1A3A38]"
          style={{ fontFamily: 'Nunito, sans-serif' }}
        >
          Semantic<span className="text-[#0D9488]">Gateway</span>
        </span>
      </div>

      {/* ── Section label ── */}
      <div className="px-4">
        <span
          className="text-[10px] font-bold tracking-widest text-[#4A7B76] uppercase"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          Analytics
        </span>
      </div>

      {/* ── Nav items ── */}
      <nav className="flex-1 px-4 py-3 flex flex-col gap-1 overflow-y-auto">
        {NAV_ITEMS.map(({ path, label, icon: Icon, dot }) => (
          <NavLink
            key={path}
            to={path}
            end={path === '/'}
            className={({ isActive }) =>
              `flex items-center gap-3 rounded-[16px] px-4 py-3 text-sm font-bold
               transition-all duration-200 relative overflow-hidden
               ${isActive
                 ? 'text-[#115E59] font-black'
                 : 'text-[#0F766E] hover:text-[#1A3A38] hover:-translate-y-0.5'
               }`
            }
            style={({ isActive }) => isActive ? {
              background: 'linear-gradient(135deg, rgba(240,253,250,0.9), rgba(255,255,255,0.95))',
              boxShadow: `
                16px 16px 32px rgba(13, 148, 136, 0.12),
                -10px -10px 24px rgba(255, 255, 255, 0.9),
                inset 6px 6px 12px rgba(13, 148, 136, 0.04),
                inset -6px -6px 12px rgba(255, 255, 255, 1)
              `,
            } : {}}
          >
            {({ isActive }) => (
              <>
                {/* Active accent bar */}
                {isActive && (
                  <span className="absolute left-0 top-2 bottom-2 w-1 rounded-full bg-[#115E59]" />
                )}
                <Icon
                  size={18}
                  className={isActive ? 'text-[#115E59]' : 'text-[#0F766E] group-hover:text-[#1A3A38] transition-colors'}
                  style={!isActive ? {
                    filter: 'drop-shadow(0 1px 2px rgba(255,255,255,0.8))',
                  } : {}}
                />
                <span className="flex-1">{label}</span>
                {dot && (
                  <span
                    className={`w-2 h-2 rounded-full ${apiHealthy ? 'bg-emerald-500' : 'bg-[#4A7B76]/40'}`}
                    title={apiHealthy ? 'API Online' : 'API Offline'}
                  />
                )}
              </>
            )}
          </NavLink>
        ))}
      </nav>

      {/* ── Bottom status indicator ── */}
      <div className="px-4 py-5">
        <div
          className="flex items-center justify-center gap-2.5 px-3 py-2.5 rounded-[20px]"
          style={{
            background: 'rgba(255,255,255,0.6)',
            boxShadow: 'inset 4px 4px 8px rgba(13,148,136,0.06), inset -4px -4px 8px rgba(255,255,255,0.9)',
          }}
        >
          <span
            className={`w-2 h-2 rounded-full shrink-0 animate-clay-breathe ${apiHealthy ? 'bg-emerald-500' : 'bg-[#4A7B76]/40'}`}
          />
          <span className="text-[11px] font-medium text-[#4A7B76] truncate">
            {apiHealthy ? 'Gateway Online' : 'Gateway Offline'}
          </span>
        </div>
      </div>
    </aside>
  );
}
