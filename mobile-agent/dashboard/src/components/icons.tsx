import { SVGProps } from "react";

/**
 * Lucide-style stroke icons, inlined so the dashboard ships zero icon deps and
 * every glyph inherits currentColor + a consistent 1.7 stroke. Used for all
 * structural UI (nav, headers, buttons); app emojis from data stay as-is since
 * those are content, not chrome.
 */

type P = SVGProps<SVGSVGElement>;
const Base = ({ children, ...p }: P & { children: React.ReactNode }) => (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.7}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    {...p}
  >
    {children}
  </svg>
);

export const Icon = {
  overview: (p: P) => (
    <Base {...p}>
      <rect x="3" y="3" width="7" height="9" rx="1.5" />
      <rect x="14" y="3" width="7" height="5" rx="1.5" />
      <rect x="14" y="12" width="7" height="9" rx="1.5" />
      <rect x="3" y="16" width="7" height="5" rx="1.5" />
    </Base>
  ),
  live: (p: P) => (
    <Base {...p}>
      <path d="M3 12h3l2.5-7 4 16 3-9H21" />
    </Base>
  ),
  history: (p: P) => (
    <Base {...p}>
      <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
      <path d="M3 4v4h4" />
      <path d="M12 8v4l3 2" />
    </Base>
  ),
  compare: (p: P) => (
    <Base {...p}>
      <path d="M7 4 3 8l4 4" />
      <path d="M3 8h13" />
      <path d="m17 20 4-4-4-4" />
      <path d="M21 16H8" />
    </Base>
  ),
  saved: (p: P) => (
    <Base {...p}>
      <path d="M6 4h12a1 1 0 0 1 1 1v15l-7-4-7 4V5a1 1 0 0 1 1-1Z" />
    </Base>
  ),
  schedules: (p: P) => (
    <Base {...p}>
      <rect x="3" y="4.5" width="18" height="16" rx="2" />
      <path d="M3 9h18M8 2.5v4M16 2.5v4" />
      <path d="M12 13v2.2l1.6 1" />
    </Base>
  ),
  apps: (p: P) => (
    <Base {...p}>
      <rect x="3" y="3" width="7" height="7" rx="1.6" />
      <rect x="14" y="3" width="7" height="7" rx="1.6" />
      <rect x="3" y="14" width="7" height="7" rx="1.6" />
      <rect x="14" y="14" width="7" height="7" rx="1.6" />
    </Base>
  ),
  settings: (p: P) => (
    <Base {...p}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 7 19.4a1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0-1.2-2.9H1a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 2.6 7a1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 7 2.6h.1A1.7 1.7 0 0 0 8.3 1V1a2 2 0 1 1 4 0v.1A1.7 1.7 0 0 0 15 2.6a1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V7a1.7 1.7 0 0 0 1.5 1H23a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z" />
    </Base>
  ),
  arrow: (p: P) => (
    <Base {...p}>
      <path d="M5 12h14M13 6l6 6-6 6" />
    </Base>
  ),
  bolt: (p: P) => (
    <Base {...p}>
      <path d="M13 2 4 14h6l-1 8 9-12h-6l1-8Z" />
    </Base>
  ),
  check: (p: P) => (
    <Base {...p}>
      <path d="M20 6 9 17l-5-5" />
    </Base>
  ),
  x: (p: P) => (
    <Base {...p}>
      <path d="M18 6 6 18M6 6l12 12" />
    </Base>
  ),
  menu: (p: P) => (
    <Base {...p}>
      <path d="M3 6h18M3 12h18M3 18h18" />
    </Base>
  ),
  mail: (p: P) => (
    <Base {...p}>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="m3 7 9 6 9-6" />
    </Base>
  ),
  calendar: (p: P) => (
    <Base {...p}>
      <rect x="3" y="4.5" width="18" height="16" rx="2" />
      <path d="M3 9h18M8 2.5v4M16 2.5v4" />
    </Base>
  ),
  trophy: (p: P) => (
    <Base {...p}>
      <path d="M8 4h8v4a4 4 0 0 1-8 0V4Z" />
      <path d="M8 5H5v1a3 3 0 0 0 3 3M16 5h3v1a3 3 0 0 1-3 3M10 13.5V17h4v-3.5M8 21h8M9 17h6" />
    </Base>
  ),
  spark: (p: P) => (
    <Base {...p}>
      <path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18" />
    </Base>
  ),
  device: (p: P) => (
    <Base {...p}>
      <rect x="6" y="2.5" width="12" height="19" rx="2.5" />
      <path d="M10.5 18.5h3" />
    </Base>
  ),
  chevronLeft: (p: P) => (
    <Base {...p}>
      <path d="m15 6-6 6 6 6" />
    </Base>
  ),
  chevronRight: (p: P) => (
    <Base {...p}>
      <path d="m9 6 6 6-6 6" />
    </Base>
  ),
};

/** The Atlas mark — an orbit ring with a live node, on a soft gradient tile. */
export function Logo({ className = "h-9 w-9" }: { className?: string }) {
  return (
    <span
      className={
        "relative grid place-items-center rounded-xl bg-brand-soft ring-1 ring-white/10 " +
        className
      }
    >
      <svg viewBox="0 0 24 24" fill="none" className="h-[60%] w-[60%]" aria-hidden="true">
        <circle cx="12" cy="12" r="8.5" stroke="url(#ag)" strokeWidth="1.7" />
        <ellipse cx="12" cy="12" rx="8.5" ry="3.4" stroke="url(#ag)" strokeWidth="1.3" opacity="0.6" />
        <circle cx="12" cy="12" r="2.6" fill="#22d3ee" />
        <defs>
          <linearGradient id="ag" x1="3" y1="4" x2="21" y2="20" gradientUnits="userSpaceOnUse">
            <stop stopColor="#22d3ee" />
            <stop offset="1" stopColor="#6366f1" />
          </linearGradient>
        </defs>
      </svg>
    </span>
  );
}
