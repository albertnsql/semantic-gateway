import React, { useState, useEffect, useRef } from 'react';
import { RotateCw, ShieldCheck, ChevronDown, Check, Filter, Database, Zap, Calendar, Cloud, Download } from 'lucide-react';
import {
  AreaChart, Area, LineChart, Line, BarChart, Bar, PieChart, Pie, Cell,
  XAxis, YAxis, Tooltip, ResponsiveContainer, Legend, CartesianGrid, Label
} from 'recharts';
import KpiTile from '../components/dashboard/KpiTile';
import ChartCard from '../components/dashboard/ChartCard';
import ChatPanel from '../components/dashboard/ChatPanel';
import LoadingSpinner from '../components/LoadingSpinner';
import { fetchDashboardWidget } from '../api/dashboard';
import { postQuery } from '../api/query';
import { Button } from '../components/ui/Button';
import { Badge } from '../components/ui/Badge';

// ── Mock Data ──────────────────────────────────────────────────────────────
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const generateMockArea    = () => MONTHS.map((name, i) => ({ name, value: Math.round(42000 * (1 + i * 0.015) + (Math.random() * 5000 - 2500)) }));
const generateMockLine    = () => MONTHS.map((name) => ({ name, value: parseFloat((4.5 + Math.random() * 1.0 - 0.5).toFixed(2)) }));
const generateMockSessions= () => MONTHS.map((name, i) => ({ name, value: Math.round(38000 * (1 + i * 0.01) + (Math.random() * 6000 - 3000)) }));
const generateMockBar     = () => [{ name:'Enterprise', value:95000 }, { name:'Pro', value:42000 }, { name:'Free', value:3400 }];
const generateMockPie     = () => [{ name:'Free', value:8500 }, { name:'Pro', value:3200 }, { name:'Enterprise', value:850 }];
const generateMockForecast = () => MONTHS.map((name, i) => ({ name, value: Math.round(90000 * (1 + i * 0.05) + (Math.random() * 8000)) }));

const PIE_COLORS = ['#0F766E', '#14B8A6', '#2DD4BF'];

const ALL_PLANS = ['basic', 'standard', 'premium'];
const ALL_YEARS = [2023, 2024, 2025, 2026];
const ALL_COUNTRIES = ['US', 'IN', 'GB', 'DE', 'BR'];

// ── Filter Dropdown Component ─────────────────────────────────────────────
function FilterDropdown({ label, options, selected, onToggle, onSelectAll, icon: Icon = Filter }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const allSelected  = selected.length === options.length;
  const someSelected = selected.length > 0 && !allSelected;
  const displayLabel = allSelected ? label : `${label}: ${selected.slice(0,2).join(', ')}${selected.length > 2 ? ` +${selected.length-2}` : ''}`;

  const CLAY_SHADOW = `16px 16px 32px rgba(13,148,136,0.12), -10px -10px 24px rgba(255,255,255,0.9), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(v => !v)}
        className="flex items-center gap-2 px-4 py-2 rounded-[20px] text-sm font-medium transition-all duration-200 hover:-translate-y-0.5 backdrop-blur-xl"
        style={{
          background: open || someSelected ? 'rgba(13,148,136,0.08)' : 'rgba(255,255,255,0.70)',
          color: open || someSelected ? '#0D9488' : '#4A7B76',
          boxShadow: CLAY_SHADOW,
        }}
      >
        <Icon size={14} className={open || someSelected ? 'text-[#0D9488]' : 'text-[#4A7B76]'} />
        {displayLabel}
        <ChevronDown size={14} className={`text-[#4A7B76] transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div
          className="absolute top-full left-0 mt-2 rounded-[24px] z-50 min-w-[200px] py-2 animate-fade-in backdrop-blur-xl"
          style={{ background: 'rgba(255,255,255,0.95)', boxShadow: CLAY_SHADOW }}
        >
          <button
            onClick={onSelectAll}
            className="w-full flex items-center gap-3 px-4 py-2 hover:bg-[#F0FAF9] text-sm font-medium text-[#1A3A38] transition-colors"
            style={{ fontFamily: 'DM Sans, sans-serif' }}
          >
            <div
              className="w-4 h-4 rounded-full flex items-center justify-center shrink-0"
              style={{
                background: allSelected ? '#0D9488' : 'transparent',
                boxShadow: 'inset 2px 2px 4px rgba(13,148,136,0.10), inset -2px -2px 4px rgba(255,255,255,0.9)',
                border: allSelected ? 'none' : '2px solid rgba(13,148,136,0.20)',
              }}
            >
              {allSelected && <Check size={10} className="text-white" />}
            </div>
            Select All
          </button>
          <div className="border-t border-[#0D9488]/08 my-1" />
          {options.map(opt => {
            const isSelected = selected.includes(opt);
            return (
              <button
                key={opt}
                onClick={() => onToggle(opt)}
                className="w-full flex items-center gap-3 px-4 py-2 hover:bg-[#F0FAF9] text-sm text-[#4A7B76] transition-colors"
                style={{ fontFamily: 'DM Sans, sans-serif' }}
              >
                <div
                  className="w-4 h-4 rounded-full flex items-center justify-center shrink-0"
                  style={{
                    background: isSelected ? '#0D9488' : 'transparent',
                    boxShadow: 'inset 2px 2px 4px rgba(13,148,136,0.10), inset -2px -2px 4px rgba(255,255,255,0.9)',
                    border: isSelected ? 'none' : '2px solid rgba(13,148,136,0.20)',
                  }}
                >
                  {isSelected && <Check size={10} className="text-white" />}
                </div>
                {opt}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

const getLatestMonthBadge = (data) => {
  if (!data || !data.length) return "● N/A";
  const latest = data[data.length - 1].name;
  if (/^[a-zA-Z]{3} \d{4}$/.test(latest)) {
    return `● ${latest.toUpperCase()}`;
  }
  const d = new Date(latest);
  if (!isNaN(d.getTime())) {
    const month = d.toLocaleString('en-US', { month: 'short' }).toUpperCase();
    return `● ${month} ${d.getFullYear()}`;
  }
  return `● ${latest}`;
};

// ── Main Component ─────────────────────────────────────────────────────────
export default function DashboardPage() {
  const [drawerOpen, setDrawerOpen]         = useState(true);
  const [globalError, setGlobalError]       = useState(false);
  const [lastRefreshed, setLastRefreshed]   = useState(new Date());

  const [selectedPlans,  setSelectedPlans]  = useState([...ALL_PLANS]);
  const [selectedYears,  setSelectedYears]  = useState([...ALL_YEARS]);
  const [selectedCountries, setSelectedCountries] = useState([...ALL_COUNTRIES]);

  const [messages, setMessages] = useState(() => {
    try {
      const saved = sessionStorage.getItem('dashboard_chat_messages');
      if (saved) {
        const parsed = JSON.parse(saved);
        return parsed.map(m => ({
          ...m,
          date: m.date ? new Date(m.date) : undefined
        }));
      }
    } catch (e) {}
    return [{
      role: 'agent', status: 'success',
      content: "Hi! I'm your Semantic Gateway. I can answer questions about MRR, churn, engagement, and LTV — all grain-validated before execution. Try one of the suggestions below. Note: Data is current through May 2026 and refreshed monthly."
    }];
  });


  const [isTyping, setIsTyping]     = useState(false);
  const [showPrompts, setShowPrompts] = useState(() => {
    try {
      const saved = sessionStorage.getItem('dashboard_chat_messages');
      if (saved) return JSON.parse(saved).length <= 1;
    } catch (e) {}
    return true;
  });
  const [chatContext, setChatContext] = useState(null);

  const [kpis, setKpis] = useState({
    mrr:        { loading: true, data: null, error: null, trend:  4.2,  trendIsGood: true  },
    subs:       { loading: true, data: null, error: null, trend:  8.5,  trendIsGood: true  },
    watchTime:  { loading: true, data: null, error: null, trend:  2.4,  trendIsGood: true  },
    newMrr:     { loading: true, data: null, error: null, trend:  8.1,  trendIsGood: true  },
    engagement: { loading: true, data: null, error: null, trend: -1.2,  trendIsGood: false },
    ltv:        { loading: true, data: null, error: null, trend:  5.4,  trendIsGood: true  },
  });

  const [charts, setCharts] = useState({
    subDist:    { loading: true, data: null, isMock: false },
    mrrPlan:    { loading: true, data: null, isMock: false },
    mrrTrend:   { loading: true, data: null, isMock: false },
    retentionTrend: { loading: true, data: null, isMock: false },
    sessions:   { loading: true, data: null, isMock: false },
    watchTimeContent: { loading: true, data: null, isMock: false },
  });

  const loadDashboardData = async (plans = selectedPlans, years = selectedYears, countries = selectedCountries) => {
    setGlobalError(false);
    setKpis(prev  => { const s={}; Object.keys(prev).forEach(k => s[k]={...prev[k], loading:true}); return s; });
    setCharts(prev => { const s={}; Object.keys(prev).forEach(k => s[k]={...prev[k], loading:true}); return s; });

    const allPlansSelected = plans.length === ALL_PLANS.length;
    const allYearsSelected = years.length === ALL_YEARS.length;
    const allCountriesSelected = countries.length === ALL_COUNTRIES.length;

    const filterPlanTypes    = allPlansSelected ? [] : plans;
    const filterYears        = allYearsSelected ? [] : years.map(Number);
    const filterCountries    = allCountriesSelected ? [] : countries;
    
    const filters = { 
      planTypes: filterPlanTypes, 
      years: filterYears,
      countries: filterCountries
    };

    const parseKpi = (res, trendIsGood) => {
      if (!res || res.status === 'rejected' || res.value?.status === 'error')
        return { loading: false, data: null, prevData: null, error: res?.value?.error || 'Unavailable', trend: null, trendIsGood };
      const rows = res.value?.data || [];
      if (!rows.length) return { loading: false, data: null, prevData: null, error: 'No data', trend: null, trendIsGood };
      
      const getVal = (r) => {
          if (!r) return null;
          if ('value' in r) return Number(r.value);
          if ('VALUE' in r) return Number(r.VALUE);
          const keys = Object.keys(r);
          return Number(r[keys.find(k => k.toLowerCase() !== 'period_month') || keys[1]]);
      };

      const currentVal = getVal(rows[0]);
      let prevVal = null;
      let calculatedTrend = null;

      if (rows.length > 1) {
          prevVal = getVal(rows[1]);
          if (prevVal && prevVal !== 0) {
              calculatedTrend = ((currentVal - prevVal) / Math.abs(prevVal)) * 100;
          }
      }

      return { 
          loading: false, 
          data: currentVal, 
          prevData: prevVal,
          error: null, 
          trend: calculatedTrend !== null ? Number(calculatedTrend.toFixed(1)) : null, 
          trendIsGood 
      };
    };

    const parseChart = (res, mockGen) => {
      if (!res || res.status === 'rejected' || res.value?.status === 'error' || !res.value?.data?.length)
        return { loading: false, data: mockGen(), isMock: true };
      try {
        const rows = res.value.data;
        if (!rows || rows.length === 0) throw new Error('No data');
        const keys = Object.keys(rows[0]);
        const xKey = keys.find(k => /month|date|period|plan|name/i.test(k)) || keys[0];
        const yKey = keys.find(k => /mrr|revenue|amount|value|rate|count|session/i.test(k) && k !== xKey) || keys[1];
        return { loading: false, data: rows.map(r => ({ name: r[xKey], value: Number(r[yKey]) })), isMock: false };
      } catch { return { loading: false, data: mockGen(), isMock: true }; }
    };

    try {
      const widget = (id) => fetchDashboardWidget(id, filters)
        .then(r => ({ status: 'fulfilled', value: r }))
        .catch(e => ({ status: 'rejected', reason: e }));

      const [r0, r1, r2, r3, r4, r5, r6, r7, r8, r9, r10, r11] = await Promise.all([
        widget('mrr_kpi'), widget('subs_kpi'), widget('watch_time_kpi'), widget('new_mrr_kpi'),
        widget('engagement_kpi'), widget('ltv_kpi'), widget('sub_dist'), widget('mrr_by_plan'),
        widget('mrr_trend'), widget('retention_trend'), widget('sessions_trend'), widget('watch_time_content_type')
      ]);

      setKpis({
        mrr:        parseKpi(r0,  true),
        subs:       parseKpi(r1,  true),
        watchTime:  parseKpi(r2,  true),
        newMrr:     parseKpi(r3,  true),
        engagement: parseKpi(r4,  true),
        ltv:        parseKpi(r5,  true),
      });
      setCharts({
        subDist:    parseChart(r6,  generateMockPie),
        mrrPlan:    parseChart(r7,  generateMockBar),
        mrrTrend:   parseChart(r8,  generateMockArea),
        retentionTrend: parseChart(r9,  generateMockLine),
        sessions:   parseChart(r10, generateMockSessions),
        watchTimeContent: parseChart(r11, generateMockBar),
      });

      setLastRefreshed(new Date());
    } catch (err) {
      setGlobalError(true);
    }
  };

  useEffect(() => { loadDashboardData(); }, []);

  useEffect(() => {
    const activeFilters = {
      plans: selectedPlans.length === ALL_PLANS.length ? ['all'] : selectedPlans,
      years: selectedYears.length === ALL_YEARS.length ? ['all'] : selectedYears,
      countries: selectedCountries.length === ALL_COUNTRIES.length ? ['all'] : selectedCountries
    };

    const fmtCurrencyRaw = v => v == null ? '—' : `$${(Number(v)).toLocaleString()}`;
    
    const visibleWidgets = [];
    const addWidget = (id, label, kpiObj, format) => {
      if (kpiObj.data != null) {
        visibleWidgets.push({
          widget_id: id,
          label: label,
          current_value: format(kpiObj.data),
          trend: kpiObj.trend != null ? `${kpiObj.trend > 0 ? '+' : ''}${kpiObj.trend}% vs last month` : null
        });
      }
    };

    addWidget('mrr_kpi', 'Total MRR', kpis.mrr, fmtCurrencyRaw);
    addWidget('subs_kpi', 'Active Subscribers', kpis.subs, v => Number(v).toLocaleString());
    addWidget('watch_time_kpi', 'Avg Watch Time', kpis.watchTime, v => `${Number(v).toFixed(1)} min`);
    addWidget('new_mrr_kpi', 'New MRR', kpis.newMrr, fmtCurrencyRaw);
    addWidget('engagement_kpi', 'Avg Engagement', kpis.engagement, v => `${Number(v).toFixed(1)}%`);
    addWidget('ltv_kpi', 'Avg LTV', kpis.ltv, v => `$${Number(v).toFixed(1)}`);

    setChatContext({
      active_filters: activeFilters,
      visible_widgets: visibleWidgets,
      data_as_of: 'May 2026',
      page: 'dashboard'
    });
  }, [selectedPlans, selectedYears, selectedCountries, kpis]);

  useEffect(() => {
    sessionStorage.setItem('dashboard_chat_messages', JSON.stringify(messages));
  }, [messages]);



  const togglePlan = (plan) => {
    const next = selectedPlans.includes(plan) ? selectedPlans.filter(p => p !== plan) : [...selectedPlans, plan];
    setSelectedPlans(next);
  };
  const selectAllPlans = () => {
    const next = selectedPlans.length === ALL_PLANS.length ? [] : [...ALL_PLANS];
    setSelectedPlans(next);
  };
  const toggleYear = (year) => {
    const next = selectedYears.includes(year) ? selectedYears.filter(y => y !== year) : [...selectedYears, year];
    setSelectedYears(next);
  };
  const selectAllYears = () => {
    const next = selectedYears.length === ALL_YEARS.length ? [] : [...ALL_YEARS];
    setSelectedYears(next);
  };

  // ── Chat handlers ─────────────────────────────────────────────────────────
  const handleSendChat = async (text) => {
    if (!text.trim()) return;
    setShowPrompts(false);
    setMessages(prev => [...prev, { role: 'user', content: text.trim(), date: new Date() }]);
    setIsTyping(true);
    try {
      const res = await postQuery(text.trim(), messages, { 
        include_sql: true, 
        include_lineage: true, 
        max_rows: 10,
        dashboard_context: chatContext
      });
      setMessages(prev => [...prev, { role: 'agent', status: res.status, raw: res, date: new Date() }]);
    } catch (err) {
      const errorMsg = err.response?.data?.message || err.response?.data?.error || err.message || 'Failed';
      setMessages(prev => [...prev, { role: 'agent', status: 'error', error: errorMsg, date: new Date() }]);
    } finally { setIsTyping(false); }
  };

  const handleClearChat = () => {
    setMessages([{ role: 'agent', status: 'success', content: "Hi! I'm your Semantic Gateway. I can answer questions about MRR, churn, engagement, and LTV — all grain-validated before execution. Try one of the suggestions below. Note: Data is current through May 2026 and refreshed monthly." }]);
    setShowPrompts(true);
    sessionStorage.removeItem('dashboard_chat_messages');
    sessionStorage.removeItem('dashboard_chat_input');
  };

  const fmtCurrency = v => v == null ? '—' : `$${(Number(v)/1000).toFixed(1)}K`;
  const fmtDollar   = v => v == null ? '—' : `$${Number(v).toFixed(1)}`;
  const fmtPct      = v => v == null ? '—' : `${Number(v).toFixed(1)}%`;
  const fmtWatchTime= v => v == null ? '—' : `${Number(v).toFixed(1)} min`;
  const fmtInt      = v => v == null ? '—' : Number(v).toLocaleString();
  const fmtKilo     = v => v == null ? ''  : `${(Number(v)/1000).toFixed(0)}K`;
  const fmtMonthYear = v => {
    if (!v) return '';
    const d = new Date(v);
    if (!isNaN(d.getTime())) {
      const month = d.toLocaleString('en-US', { month: 'short' });
      const year = d.getFullYear().toString().slice(-2);
      return `${month} ${year}`;
    }
    return v;
  };

  const filtersActive = selectedPlans.length < ALL_PLANS.length || 
                        selectedYears.length < ALL_YEARS.length ||
                        selectedCountries.length < ALL_COUNTRIES.length;

  const generateSuggestedPrompts = () => {
    const plansFiltered = selectedPlans.length !== ALL_PLANS.length && selectedPlans.length > 0;
    const yearsFiltered = selectedYears.length !== ALL_YEARS.length && selectedYears.length > 0;
    const countriesFiltered = selectedCountries.length !== ALL_COUNTRIES.length && selectedCountries.length > 0;

    const prompts = [];

    // 1. Data-driven prompt based on MRR or New MRR
    if (kpis.newMrr.data != null && kpis.newMrr.trend != null && Math.abs(kpis.newMrr.trend) > 5) {
      prompts.push(`Show me New MRR by plan type to investigate the recent change`);
    } else if (kpis.mrr.data != null && kpis.mrr.trend != null) {
      prompts.push(`Break down Total MRR by country to see the recent trend`);
    } else {
      let p = "What's our MRR this month?";
      if (plansFiltered) p = `How does MRR trend for ${selectedPlans.join(" & ")} plans?`;
      else if (countriesFiltered) p = `What's our MRR in ${selectedCountries.join(" & ")}?`;
      prompts.push(p);
    }

    // 2. Data-driven prompt based on Engagement or Watch Time
    if (kpis.engagement.data != null && kpis.engagement.trend != null && Math.abs(kpis.engagement.trend) > 1) {
      prompts.push(`Show me average engagement by content type over the last 3 months`);
    } else if (kpis.watchTime.data != null && kpis.watchTime.trend != null) {
      prompts.push(`Break down average watch time by content type`);
    } else {
      let p = "Which content has the highest engagement?";
      if (countriesFiltered) p = `Which content has the highest engagement in ${selectedCountries.join(" & ")}?`;
      prompts.push(p);
    }

    // 3. Churn / Subs related prompt
    if (kpis.subs.data != null && kpis.subs.trend != null && kpis.subs.trend < 0) {
      prompts.push(`Show me churned subscribers by plan type for this month`);
    } else {
      let p = "Show churn by plan type";
      if (yearsFiltered) p = `Show churn by plan type for ${selectedYears.join(" & ")}`;
      if (plansFiltered) p = `What's the churn rate for ${selectedPlans.join(" & ")} users?`;
      prompts.push(p);
    }

    return prompts.slice(0, 3);
  };

  return (
    <div className="w-full flex flex-row items-start gap-5 font-sans animate-fade-in">
      <div className={`flex-1 flex flex-col min-w-0 transition-all duration-300`}>
        {globalError && (
          <div className="px-5 py-3 flex items-center gap-3 rounded-[24px] mb-4 backdrop-blur-xl" style={{ background: 'rgba(244,63,94,0.08)', boxShadow: '8px 8px 20px rgba(244,63,94,0.08), -6px -6px 16px rgba(255,255,255,0.85)' }}>
            <span className="text-[#F43F5E] font-semibold text-sm" style={{ fontFamily: 'DM Sans, sans-serif' }}>⚠ Cannot reach Semantic Gateway</span>
            <button onClick={() => loadDashboardData()} className="ml-auto text-xs text-[#F43F5E] px-4 py-1.5 rounded-[16px] font-medium backdrop-blur-xl" style={{ background: 'rgba(255,255,255,0.80)', boxShadow: '8px 8px 16px rgba(244,63,94,0.10), -6px -6px 12px rgba(255,255,255,0.9)' }}>Retry</button>
          </div>
        )}
        <div className="flex flex-col gap-5 mb-6">
          <div className="flex justify-between items-start gap-4">
            <div>
              <h1 className="text-4xl font-black text-[#1A3A38] tracking-tight" style={{ fontFamily: 'Nunito, sans-serif' }}>Executive Overview</h1>
              <p className="text-sm text-[#4A7B76] mt-1" style={{ fontFamily: 'DM Sans, sans-serif' }}>Key metrics across your streaming SaaS business</p>
              <p className="text-xs text-gray-400 mt-1" style={{ fontFamily: 'DM Sans, sans-serif' }}>⚠ Data available through May 2026</p>
            </div>
            <div className="flex items-center gap-3 flex-wrap justify-end">
              <span
                className="flex items-center gap-1.5 text-xs text-[#4A7B76]"
                style={{ fontFamily: 'DM Sans, sans-serif' }}
              >
                <Calendar size={12} className="text-[#4A7B76]" />
                Data through May 2026
              </span>
              <Button variant="outline" onClick={() => loadDashboardData(selectedPlans, selectedYears, selectedCountries)} title="Force refresh">
                <RotateCw size={14} className="text-slate-500" /> Refresh
              </Button>
            </div>
          </div>

          {/* Second Row: Filters & Controls */}
          <div
            className="relative z-40 flex items-center gap-3 flex-wrap p-3 rounded-[24px] backdrop-blur-xl"
            style={{
              background: 'rgba(255,255,255,0.65)',
              boxShadow: '16px 16px 32px rgba(13,148,136,0.08), -10px -10px 24px rgba(255,255,255,0.9), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)',
            }}
          >
            <FilterDropdown label="Plans" options={ALL_PLANS} selected={selectedPlans} onToggle={togglePlan} onSelectAll={selectAllPlans} icon={Filter} />
            <FilterDropdown label="Years" options={ALL_YEARS} selected={selectedYears} onToggle={toggleYear} onSelectAll={selectAllYears} icon={Calendar} />

            <div className="h-6 w-px mx-1" style={{ background: 'rgba(13,148,136,0.15)' }} />

            <FilterDropdown 
              label="Country" 
              options={ALL_COUNTRIES} 
              selected={selectedCountries}
              onToggle={(c) => {
                const next = selectedCountries.includes(c) ? selectedCountries.filter(x => x !== c) : [...selectedCountries, c];
                setSelectedCountries(next);
              }}
              onSelectAll={() => {
                const next = selectedCountries.length === ALL_COUNTRIES.length ? [] : [...ALL_COUNTRIES];
                setSelectedCountries(next);
              }}
              icon={Cloud}
            />

            <button
              onClick={() => loadDashboardData()}
              className="ml-1 px-4 py-1.5 rounded-full text-sm font-bold text-white bg-[#0D9488] hover:bg-[#0F766E] transition-all duration-200 shadow-md hover:-translate-y-0.5"
              style={{ fontFamily: 'DM Sans, sans-serif' }}
            >
              Apply Filters
            </button>

            <div className="ml-auto flex items-center gap-3 px-2">
              {filtersActive && (
                <button
                  onClick={() => { 
                    setSelectedPlans([...ALL_PLANS]); 
                    setSelectedYears([...ALL_YEARS]); 
                    setSelectedCountries([...ALL_COUNTRIES]);
                    loadDashboardData([...ALL_PLANS], [...ALL_YEARS], [...ALL_COUNTRIES]); 
                  }}
                  className="text-sm text-[#0D9488] hover:text-[#0D9488]/80 font-bold transition-colors"
                  style={{ fontFamily: 'DM Sans, sans-serif' }}
                >
                  Reset
                </button>
              )}
              <span
                className="inline-flex items-center gap-1.5 text-xs font-bold text-emerald-600 px-3 py-1.5 rounded-full"
                style={{ background: 'rgba(16,185,129,0.10)', boxShadow: 'inset 2px 2px 4px rgba(16,185,129,0.08), inset -2px -2px 4px rgba(255,255,255,0.9)' }}
              >
                <ShieldCheck size={12} /> Metrics Certified
              </span>
            </div>
          </div>
        </div>

        <div className="flex flex-col gap-6">

          {/* ── Row 2: KPIs ─────────────────────────────────────────────── */}
          <div>
            {filtersActive && (
              <div className="flex items-center justify-end gap-1.5 text-xs font-bold text-amber-600 mb-2 px-1 animate-fade-in">
                <Filter size={12} /> Filtered View Active
              </div>
            )}
            <div className={`grid gap-4 ${drawerOpen ? 'grid-cols-2 xl:grid-cols-3' : 'grid-cols-2 lg:grid-cols-3 xl:grid-cols-6'}`}>
              <KpiTile label="TOTAL MRR"          value={fmtCurrency(kpis.mrr.data)}        prevValue={kpis.mrr.prevData}        formatter={fmtCurrency}  trend={kpis.mrr.trend}        trendIsGood={kpis.mrr.trendIsGood}        loading={kpis.mrr.loading}        error={kpis.mrr.error} />
              <KpiTile label="ACTIVE SUBSCRIBERS" value={fmtInt(kpis.subs.data)}             prevValue={kpis.subs.prevData}       formatter={fmtInt}       trend={kpis.subs.trend}       trendIsGood={kpis.subs.trendIsGood}       loading={kpis.subs.loading}       error={kpis.subs.error} />
              <KpiTile label="AVG WATCH TIME"     value={fmtWatchTime(kpis.watchTime.data)}  prevValue={kpis.watchTime.prevData}  formatter={fmtWatchTime} trend={kpis.watchTime.trend}  trendIsGood={kpis.watchTime.trendIsGood}  loading={kpis.watchTime.loading}  error={kpis.watchTime.error} />
              <KpiTile label="NEW MRR"            value={fmtCurrency(kpis.newMrr.data)}      prevValue={kpis.newMrr.prevData}     formatter={fmtCurrency}  trend={kpis.newMrr.trend}     trendIsGood={kpis.newMrr.trendIsGood}     loading={kpis.newMrr.loading}     error={kpis.newMrr.error} />
              <KpiTile label="AVG ENGAGEMENT"     value={fmtPct(kpis.engagement.data)}       prevValue={kpis.engagement.prevData} formatter={fmtPct}       trend={kpis.engagement.trend} trendIsGood={kpis.engagement.trendIsGood} loading={kpis.engagement.loading} error={kpis.engagement.error} />
              <KpiTile label="AVG LTV"            value={fmtDollar(kpis.ltv.data)}           prevValue={kpis.ltv.prevData}        formatter={fmtDollar}    trend={kpis.ltv.trend}        trendIsGood={kpis.ltv.trendIsGood}        loading={kpis.ltv.loading}        error={kpis.ltv.error} />
            </div>
          </div>

          {/* ── Row 3: Pie + Bar ──────────────────────────────────────── */}
          <div className="grid grid-cols-2 gap-4 h-[340px]">
            <div className="h-full min-w-0">
              <ChartCard title="Subscriber Distribution" isMock={charts.subDist.isMock}>
                {charts.subDist.loading ? <LoadingSpinner /> : (
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie data={charts.subDist.data} cx="50%" cy="48%" innerRadius={68} outerRadius={96} dataKey="value" stroke="none">
                        {charts.subDist.data?.map((_, i) => <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />)}
                      </Pie>
                      <Tooltip content={<LightTooltip formatter={fmtInt} />} />
                      <Legend content={<PieLegend total={charts.subDist.data?.reduce((a,b)=>a+b.value,0) ?? 0} />} verticalAlign="bottom" />
                    </PieChart>
                    {/* Absolute Center Text Overlay to avoid SVG Label bugs */}
                    <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none pb-6">
                      <span className="text-[26px] font-black text-[#1A3A38] leading-none" style={{ fontFamily:'Nunito, sans-serif' }}>
                        {fmtInt(charts.subDist.data?.reduce((a,b)=>a+b.value,0))}
                      </span>
                      <span className="text-[10px] font-bold text-[#4A7B76] uppercase tracking-wider mt-1">total users</span>
                    </div>
                  </ResponsiveContainer>
                )}
              </ChartCard>
            </div>
            <div className="h-full min-w-0">
              <ChartCard title="MRR by Plan" isMock={charts.mrrPlan.isMock}>
                {charts.mrrPlan.loading ? <LoadingSpinner /> : (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={charts.mrrPlan.data} margin={{ top:16, right:16, left:0, bottom:0 }} maxBarSize={60}>
                      <CartesianGrid vertical={false} stroke="rgba(13,148,136,0.08)" />
                      <XAxis dataKey="name" axisLine={false} tickLine={false} fontSize={12} tick={{ fill:'#4A7B76', fontWeight: 500 }} dy={10} />
                      <YAxis domain={[0, 'dataMax']} axisLine={false} tickLine={false} fontSize={11} tick={{ fill:'#4A7B76', fontWeight: 500 }} tickFormatter={fmtKilo} />
                      <Tooltip cursor={{ fill:'#F8FAFC' }} content={<LightTooltip formatter={fmtCurrency} />} />
                      <Bar dataKey="value" name="MRR" radius={[8, 8, 0, 0]}>
                        {charts.mrrPlan.data?.map((_, i) => <Cell key={i} fill={PIE_COLORS[i%3]} />)}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                )}
              </ChartCard>
            </div>
          </div>

          {/* ── Row 4: Area + Line ────────────────────────────────────── */}
          <div className="grid grid-cols-[3fr_2fr] gap-4 h-[320px]">
            <div className="h-full min-w-0">
              <ChartCard title="MRR Trend (12 months)" badge={getLatestMonthBadge(charts.mrrTrend.data)} isMock={charts.mrrTrend.isMock}>
                {charts.mrrTrend.loading ? <LoadingSpinner /> : (
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={charts.mrrTrend.data?.filter(d => d.name <= '2026-05-01')} margin={{ top:8, right:10, left:-8, bottom:0 }}>
                      <defs>
                        <linearGradient id="gMrr" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%"   stopColor="#0F766E" stopOpacity={0.2} />
                          <stop offset="100%" stopColor="#0F766E" stopOpacity={0}    />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="rgba(13,148,136,0.08)" />
                      <XAxis dataKey="name" axisLine={false} tickLine={false} fontSize={11} tick={{ fill:'#4A7B76', fontWeight: 500 }} dy={10} tickFormatter={fmtMonthYear} />
                      <YAxis axisLine={false} tickLine={false} fontSize={11} tick={{ fill:'#4A7B76', fontWeight: 500 }} tickFormatter={fmtKilo} />
                      <Tooltip content={<LightTooltip formatter={fmtCurrency} labelFormatter={fmtMonthYear} />} />
                      <Area type="monotone" dataKey="value" stroke="#0F766E" strokeWidth={2.5} fill="url(#gMrr)" dot={false} activeDot={{ r:5, fill:'#0F766E', strokeWidth:0 }} />
                    </AreaChart>
                  </ResponsiveContainer>
                )}
              </ChartCard>
            </div>
            <div className="h-full min-w-0">
              <ChartCard title="Retention Rate Trend (12 months)" badge={getLatestMonthBadge(charts.retentionTrend.data)} isMock={charts.retentionTrend.isMock}>
                {charts.retentionTrend.loading ? <LoadingSpinner /> : (
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={charts.retentionTrend.data} margin={{ top:8, right:10, left:-8, bottom:0 }}>
                      <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="rgba(13,148,136,0.08)" />
                      <XAxis dataKey="name" axisLine={false} tickLine={false} fontSize={11} tick={{ fill:'#4A7B76', fontWeight: 500 }} dy={10} tickFormatter={fmtMonthYear} />
                      <YAxis domain={[0, 100]} axisLine={false} tickLine={false} fontSize={11} tick={{ fill:'#4A7B76', fontWeight: 500 }} tickFormatter={fmtPct} />
                      <Tooltip content={<LightTooltip formatter={fmtPct} labelFormatter={fmtMonthYear} />} />
                      <Line type="monotone" dataKey="value" stroke="#0F766E" strokeWidth={2.5} dot={{ r:3, fill:'#0F766E', strokeWidth:0 }} activeDot={{ r:5, fill:'#0F766E', strokeWidth:0 }} />
                    </LineChart>
                  </ResponsiveContainer>
                )}
              </ChartCard>
            </div>
          </div>

          {/* ── Row 5: Sessions + Forecast ───────────────────────────── */}
          <div className="grid grid-cols-2 gap-4 h-[300px]">
            <div className="h-full min-w-0">
              <ChartCard title="Monthly Stream Sessions" badge={getLatestMonthBadge(charts.sessions.data)} isMock={charts.sessions.isMock}>
                {charts.sessions.loading ? <LoadingSpinner /> : (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={charts.sessions.data} margin={{ top:8, right:10, left:-8, bottom:0 }} maxBarSize={40}>
                      <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="rgba(13,148,136,0.08)" />
                      <XAxis dataKey="name" axisLine={false} tickLine={false} fontSize={11} tick={{ fill:'#4A7B76', fontWeight: 500 }} dy={10} tickFormatter={fmtMonthYear} />
                      <YAxis axisLine={false} tickLine={false} fontSize={11} tick={{ fill:'#4A7B76', fontWeight: 500 }} tickFormatter={fmtKilo} />
                      <Tooltip cursor={{ fill:'rgba(13,148,136,0.04)' }} content={<LightTooltip formatter={fmtInt} labelFormatter={fmtMonthYear} />} />
                      <Bar dataKey="value" fill="#14B8A6" radius={[8, 8, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                )}
              </ChartCard>
            </div>
            <div className="h-full min-w-0">
              <ChartCard title="Avg Watch Time by Content Type" isMock={charts.watchTimeContent.isMock}>
                {charts.watchTimeContent.loading ? <LoadingSpinner /> : (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart layout="vertical" data={charts.watchTimeContent.data} margin={{ top:8, right:10, left:100, bottom:15 }} maxBarSize={40}>
                      <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="rgba(13,148,136,0.08)" />
                      <XAxis type="number" axisLine={false} tickLine={false} fontSize={11} tick={{ fill:'#4A7B76', fontWeight: 500 }} tickFormatter={(v) => v}>
                        <Label value="Minutes" position="bottom" fill="#4A7B76" fontSize={11} offset={0} />
                      </XAxis>
                      <YAxis type="category" dataKey="name" axisLine={false} tickLine={false} fontSize={11} tick={{ fill:'#4A7B76', fontWeight: 500, textTransform: 'capitalize' }} />
                      <Tooltip cursor={{ fill:'rgba(13,148,136,0.04)' }} content={<LightTooltip formatter={(v) => v} />} />
                      <Bar dataKey="value" name="Avg Watch Time" fill="#0F766E" radius={[0, 8, 8, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                )}
              </ChartCard>
            </div>
          </div>

        </div>
      </div>

      {/* ── Chat Side Panel ──────────────────────────────────────────── */}
      <ChatPanel
        drawerOpen={drawerOpen}
        setDrawerOpen={setDrawerOpen}
        messages={messages}

        isTyping={isTyping}
        handleSendChat={handleSendChat}
        handleClearChat={handleClearChat}
        showPrompts={showPrompts}
        suggestedPrompts={generateSuggestedPrompts()}
      />

    </div>
  );
}

// ── Shared Sub-components ──────────────────────────────────────────────────
const CLAY_TT_SHADOW = `16px 16px 32px rgba(13,148,136,0.12), -10px -10px 24px rgba(255,255,255,0.9), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;

function LightTooltip({ active, payload, label, formatter, labelFormatter }) {
  if (!active || !payload?.length) return null;
  let name = payload[0].payload?.name || payload[0].name || label;
  if (labelFormatter) name = labelFormatter(name);
  return (
    <div
      className="rounded-[20px] px-4 py-3 text-sm z-50 backdrop-blur-xl"
      style={{ background: 'rgba(255,255,255,0.95)', boxShadow: CLAY_TT_SHADOW, fontFamily: 'DM Sans, sans-serif' }}
    >
      {name && <p className="text-[#4A7B76] text-xs mb-1.5 font-bold uppercase tracking-wider">{name}</p>}
      <p className="text-[#1A3A38] font-black text-base" style={{ fontFamily: 'Nunito, sans-serif' }}>
        {formatter ? formatter(payload[0].value) : payload[0].value}
      </p>
    </div>
  );
}

function PieLegend({ payload, total }) {
  if (!payload) return null;
  return (
    <div className="flex justify-center gap-5 mt-4 flex-wrap">
      {payload.map((entry, i) => {
        const pct = total > 0 ? ((entry.payload.value / total) * 100).toFixed(1) : 0;
        return (
          <div key={i} className="flex items-center gap-2 text-sm text-[#4A7B76]" style={{ fontFamily: 'DM Sans, sans-serif' }}>
            <div className="w-3 h-3 rounded-full flex-shrink-0" style={{ backgroundColor: entry.color }} />
            <span className="font-medium">{entry.value}</span>
            <span className="text-[#4A7B76]/60">({pct}%)</span>
          </div>
        );
      })}
    </div>
  );
}
