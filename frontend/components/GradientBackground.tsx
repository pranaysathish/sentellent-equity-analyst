"use client";

import { useEffect, useRef } from "react";

interface Props {
  /** 0 disables the grain entirely. Around 0.5 is a texture; 1.5 is visible noise. */
  intensity?: number;
  /** Tile size in pixels. Larger repeats less obviously but costs more per frame. */
  patternSize?: number;
  /** Redraw every Nth frame. 1 is every frame; 3 is ~20fps and near-indistinguishable. */
  refreshInterval?: number;
  /** Grain opacity, 0-255. */
  alpha?: number;
}

/**
 * An animated grain layer over a radial gradient.
 *
 * The gradient alone reads as a flat wash on an OLED-black page and bands
 * badly on 8-bit displays. A moving grain breaks up both — it is the same
 * trick film emulation uses, and it costs one canvas.
 *
 * Written against the project's CSS variables rather than a utility framework,
 * so it inherits the palette automatically: change the accent token and this
 * follows.
 *
 * Three things the usual implementation of this gets wrong, all handled here:
 * it runs a requestAnimationFrame loop forever even in a background tab, it
 * ignores `prefers-reduced-motion` despite being a full-screen animation, and
 * it regenerates the noise every single frame when every third is visually
 * identical.
 */
export function GradientBackground({
  intensity = 0.55,
  patternSize = 110,
  refreshInterval = 3,
  alpha = 16,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const ctx = canvas.getContext("2d", { alpha: true });
    if (!ctx) return;

    // The grain is generated once into a small offscreen tile and then repeated,
    // rather than filling the viewport pixel by pixel.
    const tile = document.createElement("canvas");
    tile.width = patternSize;
    tile.height = patternSize;
    const tileCtx = tile.getContext("2d");
    if (!tileCtx) return;

    const image = tileCtx.createImageData(patternSize, patternSize);
    const pixels = image.data;

    let cssWidth = 0;
    let cssHeight = 0;

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2); // 3x costs a lot for grain
      cssWidth = window.innerWidth;
      cssHeight = window.innerHeight;
      canvas.width = Math.floor(cssWidth * dpr);
      canvas.height = Math.floor(cssHeight * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const regenerate = () => {
      for (let i = 0; i < pixels.length; i += 4) {
        const value = Math.random() * 255 * intensity;
        pixels[i] = pixels[i + 1] = pixels[i + 2] = value;
        pixels[i + 3] = alpha;
      }
      tileCtx.putImageData(image, 0, 0);
    };

    const paint = () => {
      if (cssWidth === 0 || cssHeight === 0) return;
      ctx.clearRect(0, 0, cssWidth, cssHeight);
      const pattern = ctx.createPattern(tile, "repeat");
      if (!pattern) return;
      ctx.fillStyle = pattern;
      ctx.fillRect(0, 0, cssWidth, cssHeight);
    };

    resize();
    regenerate();
    paint();

    let raf = 0;
    let frame = 0;

    const loop = () => {
      // Nothing is visible in a hidden tab, so the loop parks itself rather
      // than burning a core behind another window.
      if (document.hidden) {
        raf = window.requestAnimationFrame(loop);
        return;
      }
      if (frame % refreshInterval === 0) {
        regenerate();
        paint();
      }
      frame++;
      raf = window.requestAnimationFrame(loop);
    };

    // A static grain still breaks up the gradient banding, so reduced-motion
    // users keep the texture and lose only the movement.
    if (!reduceMotion) loop();

    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      if (raf) window.cancelAnimationFrame(raf);
    };
  }, [intensity, patternSize, refreshInterval, alpha]);

  return (
    <div className="bg-layer" aria-hidden>
      <div className="bg-gradient" />
      <div className="bg-glow" />
      <canvas ref={canvasRef} className="bg-noise" />
    </div>
  );
}
