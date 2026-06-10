import { useState } from "react";
import { withToken } from "../api";

export interface CanvasStep {
  step: number;
  screenshotUrl: string | null;
  coords: {
    tap?: { x: number; y: number };
    swipe?: { x1: number; y1: number; x2: number; y2: number };
    text?: string;
  };
  note: string;
  rejected: boolean;
  action_type: string | null;
}

const ACCENT = "#22d3ee";
const RED = "#f87171";

/**
 * Renders the agent's current screen in a phone frame with an SVG overlay that
 * marks the tap/swipe and draws a faint trail of recent taps — the "movement".
 *
 * Coordinates are device pixels == screenshot pixels, so we set the SVG viewBox
 * to the image's natural size; the browser scales overlay + image together (no
 * manual scale math). Shared by the live view and the historical replay.
 */
export function DeviceCanvas({
  frames,
  focusedIndex,
}: {
  frames: CanvasStep[];
  focusedIndex: number;
}) {
  const [dims, setDims] = useState<{ w: number; h: number } | null>(null);
  const focused =
    frames[focusedIndex] ?? frames[frames.length - 1] ?? null;
  const src = focused?.screenshotUrl ? withToken(focused.screenshotUrl) : null;
  const color = focused?.rejected ? RED : ACCENT;

  const tap = focused?.coords.tap;
  const swipe = focused?.coords.swipe;
  const trail = frames
    .slice(0, focusedIndex + 1)
    .map((f) => f.coords.tap)
    .filter((p): p is { x: number; y: number } => !!p);

  return (
    <div className="mx-auto w-full max-w-[300px]">
      <div
        style={{ aspectRatio: dims ? `${dims.w} / ${dims.h}` : "9 / 19.5" }}
        className="relative overflow-hidden rounded-[2rem] bg-black ring-4 ring-zinc-800 shadow-2xl shadow-black/50"
      >
        {src ? (
          <img
            key={focused?.step}
            src={src}
            alt={`step ${focused?.step}`}
            onLoad={(e) =>
              setDims({
                w: e.currentTarget.naturalWidth,
                h: e.currentTarget.naturalHeight,
              })
            }
            className="absolute inset-0 h-full w-full object-contain"
          />
        ) : (
          <div className="grid h-full place-items-center text-sm text-zinc-600">
            waiting for the screen…
          </div>
        )}

        {src && dims && (
          <svg
            viewBox={`0 0 ${dims.w} ${dims.h}`}
            preserveAspectRatio="xMidYMid meet"
            className="pointer-events-none absolute inset-0 h-full w-full"
          >
            {trail.length > 1 && (
              <polyline
                points={trail.map((p) => `${p.x},${p.y}`).join(" ")}
                fill="none"
                stroke="rgba(34,211,238,0.30)"
                strokeWidth={6}
                strokeDasharray="16 12"
                strokeLinejoin="round"
              />
            )}
            {trail.slice(0, -1).map((p, i) => (
              <circle key={i} cx={p.x} cy={p.y} r={10} fill="rgba(34,211,238,0.25)" />
            ))}
            {swipe && (
              <g stroke={color} strokeWidth={8} strokeLinecap="round" fill="none">
                <line x1={swipe.x1} y1={swipe.y1} x2={swipe.x2} y2={swipe.y2} />
                <circle cx={swipe.x1} cy={swipe.y1} r={14} fill={color} />
                <circle cx={swipe.x2} cy={swipe.y2} r={20} fill="none" />
              </g>
            )}
            {tap && (
              <g>
                <circle cx={tap.x} cy={tap.y} r={46} fill="none" stroke={color} strokeWidth={6} opacity={0.5} />
                <circle cx={tap.x} cy={tap.y} r={18} fill={color} />
              </g>
            )}
          </svg>
        )}
      </div>

      {focused && (
        <div className="mt-3 text-center text-sm">
          <span className="rounded-full bg-white/5 px-2 py-0.5 font-mono text-xs text-zinc-400">
            step {focused.step}
          </span>
          <span className="ml-2 text-zinc-300">
            {focused.action_type ?? "—"}
          </span>
          {focused.note && (
            <div className="mt-1 text-xs text-zinc-500">{focused.note}</div>
          )}
          {focused.rejected && (
            <div className="mt-1 text-xs text-red-400">rejected · retried</div>
          )}
        </div>
      )}
    </div>
  );
}
