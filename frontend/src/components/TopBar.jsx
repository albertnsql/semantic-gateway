/** components/TopBar.jsx — page title + optional breadcrumb. Claymorphism theme. */
export default function TopBar({ title, breadcrumb = [] }) {
  return (
    <div className="mb-2 flex flex-col gap-1">
      {breadcrumb.length > 0 && (
        <div className="flex items-center gap-2 mb-1">
          {breadcrumb.map((crumb, idx) => (
            <span key={idx} className="flex items-center gap-2">
              <span
                className="text-xs font-medium text-[#4A7B76]"
                style={{ fontFamily: 'DM Sans, sans-serif' }}
              >
                {crumb}
              </span>
              {idx < breadcrumb.length - 1 && (
                <span className="text-[#4A7B76]/50 text-xs">/</span>
              )}
            </span>
          ))}
        </div>
      )}
      <h1
        className="text-4xl font-black tracking-tight text-[#1A3A38]"
        style={{ fontFamily: 'Nunito, sans-serif' }}
      >
        {title}
      </h1>
    </div>
  );
}
