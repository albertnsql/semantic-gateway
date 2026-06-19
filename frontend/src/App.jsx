/**
 * App.jsx — root component with router, sidebar layout, health check,
 * and claymorphism floating background blobs.
 */
import { useState, useEffect } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import Sidebar from './components/Sidebar';
import LandingPage          from './pages/LandingPage';
import QueryPage            from './pages/QueryPage';
import DashboardPage        from './pages/DashboardPage';
import MetricsCatalogPage   from './pages/MetricsCatalogPage';
import LineageExplorerPage  from './pages/LineageExplorerPage';
import DemoScenariosPage    from './pages/DemoScenariosPage';
import HowItWorksPage       from './pages/HowItWorksPage';
import { getHealth } from './api/metrics';

function Layout({ children, apiHealthy }) {
  return (
    <div
      className="flex h-screen overflow-hidden selection:bg-[#0D9488] selection:text-white"
      style={{ backgroundColor: 'var(--clay-canvas)', color: 'var(--clay-foreground)', fontFamily: "'DM Sans', sans-serif" }}
    >
      {/* ── Animated teal blobs ── */}
      <div className="pointer-events-none fixed inset-0 overflow-hidden" style={{ zIndex: 0 }}>
        <div
          className="absolute rounded-full animate-clay-float"
          style={{
            top: '-15%', left: '-10%',
            height: '65vh', width: '65vh',
            background: 'rgba(13,148,136,0.10)',
            filter: 'blur(80px)',
          }}
        />
        <div
          className="absolute rounded-full animate-clay-float-alt animation-delay-2000"
          style={{
            right: '-10%', top: '20%',
            height: '55vh', width: '55vh',
            background: 'rgba(45,212,191,0.10)',
            filter: 'blur(80px)',
          }}
        />
        <div
          className="absolute rounded-full animate-clay-float-slow animation-delay-4000"
          style={{
            bottom: '-10%', left: '30%',
            height: '50vh', width: '50vh',
            background: 'rgba(8,145,178,0.08)',
            filter: 'blur(80px)',
          }}
        />
      </div>

      <Sidebar apiHealthy={apiHealthy} />
      <main
        className="flex-1 h-screen min-w-0 overflow-y-auto relative"
        style={{ zIndex: 1 }}
      >
        <div className="w-full max-w-[1600px] mx-auto pl-8 pr-4 py-6">
          {children}
        </div>
      </main>
    </div>
  );
}

export default function App() {
  const [apiHealthy, setApiHealthy] = useState(false);

  useEffect(() => {
    getHealth()
      .then((data) => setApiHealthy(data.status === 'healthy' || data.status === 'degraded'))
      .catch(() => setApiHealthy(false));
  }, []);

  return (
    <BrowserRouter>
      <Layout apiHealthy={apiHealthy}>
        <Routes>
          <Route path="/"            element={<LandingPage />} />
          <Route path="/how-it-works" element={<HowItWorksPage />} />
          <Route path="/dashboard"    element={<DashboardPage />} />
          <Route path="/query"        element={<QueryPage />} />
          <Route path="/metrics"      element={<MetricsCatalogPage />} />
          <Route path="/lineage"      element={<LineageExplorerPage />} />
          <Route path="/demo"         element={<DemoScenariosPage />} />
          {/* Catch-all redirect to overview */}
          <Route path="*"        element={<LandingPage />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  );
}
