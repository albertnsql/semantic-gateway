/** @type {import('tailwindcss').Config} */
export default {
  content: [
    './index.html',
    './src/**/*.{js,ts,jsx,tsx}',
  ],
  theme: {
    extend: {
      fontFamily: {
        display: ['"Nunito"', 'ui-sans-serif', 'sans-serif'],
        nunito:  ['"Nunito"', 'ui-sans-serif', 'sans-serif'],
        dm:      ['"DM Sans"', 'ui-sans-serif', 'sans-serif'],
        sans:    ['"DM Sans"', 'ui-sans-serif', 'sans-serif'],
        mono:    ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      colors: {
        clay: {
          canvas:      '#F0FAF9',
          cardBg:      'rgba(255,255,255,0.65)',
          foreground:  '#1A3A38',
          muted:       '#4A7B76',
          accent:      '#0D9488',
          accentLight: '#2DD4BF',
          accentAlt:   '#0891B2',
          success:     '#10B981',
          warning:     '#F59E0B',
          danger:      '#F43F5E',
        },
        // Keep brand namespace for backward-compat references in charts
        brand: {
          teal:  '#0D9488',
          cyan:  '#2DD4BF',
          amber: '#F59E0B',
        },
        teal: {
          50:  '#F0FDFA',
          100: '#CCFBF1',
          200: '#99F6E4',
          300: '#5EEAD4',
          400: '#2DD4BF',
          500: '#14B8A6',
          600: '#0D9488',
          700: '#0F766E',
          800: '#115E59',
          900: '#134E4A',
        },
        semantic: {
          success: '#10B981',
          warning: '#F59E0B',
          danger:  '#F43F5E',
          info:    '#0891B2',
        },
        // Keep neu namespace so old references don't break
        neu: {
          base:             '#F0FAF9',
          primary:          '#1A3A38',
          muted:            '#4A7B76',
          accent:           '#0D9488',
          'accent-light':   '#E6F7F6',
          'accent-secondary':'#E2E8F0',
        },
      },
      backgroundImage: {
        'gradient-radial': 'radial-gradient(var(--tw-gradient-stops))',
        'clay-primary':    'linear-gradient(135deg, #2DD4BF, #0D9488)',
      },
      boxShadow: {
        // ── Clay shadow system ──────────────────────────────────────
        clayCard: `
          16px 16px 32px rgba(13, 148, 136, 0.12),
          -10px -10px 24px rgba(255, 255, 255, 0.9),
          inset 6px 6px 12px rgba(13, 148, 136, 0.04),
          inset -6px -6px 12px rgba(255, 255, 255, 1)
        `,
        clayCardHover: `
          20px 20px 40px rgba(13, 148, 136, 0.18),
          -12px -12px 28px rgba(255, 255, 255, 0.95),
          inset 6px 6px 12px rgba(13, 148, 136, 0.04),
          inset -6px -6px 12px rgba(255, 255, 255, 1)
        `,
        clayButton: `
          12px 12px 24px rgba(13, 148, 136, 0.30),
          -8px -8px 16px rgba(255, 255, 255, 0.4),
          inset 4px 4px 8px rgba(255, 255, 255, 0.4),
          inset -4px -4px 8px rgba(0, 0, 0, 0.08)
        `,
        clayButtonHover: `
          16px 16px 28px rgba(13, 148, 136, 0.38),
          -8px -8px 18px rgba(255, 255, 255, 0.5),
          inset 4px 4px 8px rgba(255, 255, 255, 0.5),
          inset -4px -4px 8px rgba(0, 0, 0, 0.06)
        `,
        clayPressed: `
          inset 10px 10px 20px rgba(13, 148, 136, 0.15),
          inset -10px -10px 20px rgba(255, 255, 255, 0.9)
        `,
        claySurface: `
          30px 30px 60px rgba(13, 148, 136, 0.08),
          -30px -30px 60px #ffffff,
          inset 10px 10px 20px rgba(13, 148, 136, 0.04),
          inset -10px -10px 20px rgba(255, 255, 255, 0.8)
        `,
        clayNav: `
          8px 8px 20px rgba(13, 148, 136, 0.10),
          -6px -6px 16px rgba(255, 255, 255, 0.85),
          inset 2px 2px 6px rgba(255, 255, 255, 0.6),
          inset -2px -2px 6px rgba(13, 148, 136, 0.05)
        `,
        // Legacy aliases kept for backward compat
        'neu-extruded':       '0 1px 2px rgba(0,0,0,0.04)',
        'neu-extruded-hover': '0 4px 6px -1px rgba(0,0,0,0.05), 0 2px 4px -1px rgba(0,0,0,0.03)',
        'neu-extruded-sm':    '0 1px 2px rgba(0,0,0,0.02)',
        'neu-inset':          'inset 0 1px 2px rgba(0,0,0,0.02)',
        'neu-inset-deep':     '0 4px 6px -1px rgba(0,0,0,0.05)',
        'neu-inset-sm':       'inset 0 1px 1px rgba(0,0,0,0.02)',
        'teal-glow':          '0 0 0 2px rgba(13,148,136,0.2), 0 0 8px rgba(13,148,136,0.1)',
        'card':               '0 1px 2px rgba(0,0,0,0.05)',
      },
      animation: {
        'fade-in':        'fadeIn 0.2s ease-out',
        'slide-in':       'slideIn 0.25s ease-out',
        'clay-float':     'clayFloat 8s ease-in-out infinite',
        'clay-float-alt': 'clayFloatAlt 10s ease-in-out infinite',
        'clay-float-slow':'clayFloatSlow 12s ease-in-out infinite',
        'clay-breathe':   'clayBreathe 6s ease-in-out infinite',
      },
      keyframes: {
        fadeIn:       { '0%': { opacity: 0 }, '100%': { opacity: 1 } },
        slideIn:      { '0%': { opacity: 0, transform: 'translateY(8px)' }, '100%': { opacity: 1, transform: 'translateY(0)' } },
        clayFloat:    { '0%, 100%': { transform: 'translateY(0) rotate(0deg)' }, '50%': { transform: 'translateY(-20px) rotate(2deg)' } },
        clayFloatAlt: { '0%, 100%': { transform: 'translateY(0) rotate(0deg)' }, '50%': { transform: 'translateY(-15px) rotate(-2deg)' } },
        clayFloatSlow:{ '0%, 100%': { transform: 'translateY(0) rotate(0deg)' }, '50%': { transform: 'translateY(-30px) rotate(5deg)' } },
        clayBreathe:  { '0%, 100%': { transform: 'scale(1)' }, '50%': { transform: 'scale(1.02)' } },
      },
      borderRadius: {
        '4xl': '2rem',
        '5xl': '2.5rem',
        '6xl': '3rem',
      },
    },
  },
  plugins: [],
};
