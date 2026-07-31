"use client";

import { useEffect, useId, useRef } from "react";

interface Props {
  /** Closing prices, oldest first. */
  series: number[] | string | null | undefined;
  /** Overrides the direction colour; otherwise inferred from first vs last. */
  positive?: boolean;
  height?: number;
}

/**
 * A one-year price sparkline.
 *
 * Hand-rolled rather than pulled from a charting library: this draws a single
 * path with no axes, legend, tooltip or interaction, and the smallest credible
 * chart library is ~40 KB to do the same forty lines of arithmetic.
 *
 * The line animates in by drawing itself — `stroke-dasharray` set to the path
 * length, then the offset animated to zero. The length has to be measured from
 * the rendered DOM, which is why the effect exists.
 */
function safeParse(value: string): number[] {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function Sparkline({ series, positive, height = 34 }: Props) {
  const gradientId = useId();
  const pathRef = useRef<SVGPathElement>(null);

  useEffect(() => {
    const path = pathRef.current;
    if (!path) return;
    const length = path.getTotalLength();
    path.style.strokeDasharray = `${length}`;
    path.style.setProperty("--len", `${length}`);
  }, [series]);

  // Defensive: a jsonb column can arrive as a JSON *string* if a decoder is
  // missing, and calling .filter on that throws inside render — which unmounts
  // the whole tree rather than losing one chart.
  const raw = Array.isArray(series)
    ? series
    : typeof series === "string"
      ? safeParse(series)
      : [];
  const clean = raw.map(Number).filter((n) => Number.isFinite(n));
  if (clean.length < 2) {
    return <div style={{ height }} aria-hidden />;
  }

  const width = 100; // viewBox units; the SVG scales to its container
  const min = Math.min(...clean);
  const max = Math.max(...clean);
  // A flat series would divide by zero and collapse to the top edge.
  const range = max - min || 1;
  // Inset vertically so the stroke is not clipped at the extremes.
  const pad = 3;
  const usable = height - pad * 2;

  const points = clean.map((value, i) => ({
    x: (i / (clean.length - 1)) * width,
    y: pad + (1 - (value - min) / range) * usable,
  }));

  const line = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(2)},${p.y.toFixed(2)}`)
    .join(" ");
  const area = `${line} L${width},${height} L0,${height} Z`;

  const up = positive ?? clean[clean.length - 1] >= clean[0];
  const stroke = up ? "var(--pos)" : "var(--neg)";

  return (
    <svg
      className="spark"
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      style={{ height }}
      role="img"
      aria-label={`Price trend, ${up ? "up" : "down"} over the period`}
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={stroke} stopOpacity="0.18" />
          <stop offset="100%" stopColor={stroke} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path className="area" d={area} fill={`url(#${gradientId})`} />
      <path ref={pathRef} className="line" d={line} stroke={stroke} />
    </svg>
  );
}
