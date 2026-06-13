/** components/LoadingSpinner.jsx — centered spinner, claymorphism theme. */
export default function LoadingSpinner({ label = 'Loading...' }) {
  return (
    <div className="flex flex-col items-center justify-center gap-4 py-16">
      <div
        className="w-10 h-10 rounded-full animate-spin"
        style={{
          border: '3px solid rgba(13,148,136,0.15)',
          borderTop: '3px solid #0D9488',
          boxShadow: '8px 8px 16px rgba(13,148,136,0.12), -6px -6px 12px rgba(255,255,255,0.9)',
        }}
      />
      {label && (
        <p
          className="text-sm text-[#4A7B76] font-medium"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          {label}
        </p>
      )}
    </div>
  );
}
