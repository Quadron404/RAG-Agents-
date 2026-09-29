import { useEffect, useRef, useState } from "react";

/**
 * The desktop wallpaper, with a cross-fade between choices.
 *
 * A CSS `background` cannot be transitioned, so changing one snaps.  This paints
 * the outgoing wallpaper on a second layer and dissolves the new one over it,
 * then collapses the two layers back into one the moment the fade ends.
 *
 * That collapse is the performance point: for the rest of the session there is
 * exactly one extra composited layer over the desktop, and while the live screen
 * is running there are none.  Nothing here loops, and nothing is animated while
 * the pointer is idle.
 *
 * When the selection is the default, no layer is rendered at all and the
 * stylesheet's own wallpaper is what shows -- see `wallpapers.ts`.
 */

const FADE_MS = 460;

interface Veil {
  css: string;
  on: boolean;
}

export function Wallpaper({ css }: { css: string | null }) {
  const [base, setBase] = useState<string | null>(css);
  const [veil, setVeil] = useState<Veil | null>(null);
  const timer = useRef<number>();

  useEffect(() => {
    // Already showing this one: nothing to do, and nothing to animate.
    if (css === base) return;
    window.clearTimeout(timer.current);

    if (css) {
      setVeil({ css, on: false });
      // The opacity change has to land in a later frame or the transition is
      // never scheduled and the swap stays instant.
      const raf = requestAnimationFrame(() => setVeil((v) => (v ? { ...v, on: true } : v)));
      timer.current = window.setTimeout(() => {
        setBase(css);
        setVeil(null);
      }, FADE_MS);
      return () => {
        cancelAnimationFrame(raf);
        window.clearTimeout(timer.current);
      };
    }

    // Back to the built-in wallpaper: dissolve the custom one out instead, so
    // the return trip is as smooth as the trip away.
    if (base) {
      setVeil({ css: base, on: true });
      timer.current = window.setTimeout(() => {
        setBase(null);
        setVeil(null);
      }, FADE_MS);
    }
  }, [css, base]);

  return (
    <>
      <div className="desktop__wall" aria-hidden />
      {base ? <div className="desktop__wall desktop__wall--alt" style={{ background: base }} aria-hidden /> : null}
      {veil ? (
        <div
          className="desktop__wall desktop__wall--alt desktop__wall--veil"
          style={{ background: veil.css, opacity: veil.on ? 1 : 0 }}
          aria-hidden
        />
      ) : null}
    </>
  );
}
