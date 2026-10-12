import { useEffect, useRef } from "react";
import type { Levels } from "../lib/useCall";

const RINGS = 7;

/** A wireframe globe of rotating rings. Amplitude and state are written straight to CSS vars each frame. */
export function VoiceOrb({ levels, mode }: { levels: React.MutableRefObject<Levels>; mode: string }) {
  const el = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let raf = 0;
    let smooth = 0;
    const tick = () => {
      const l = levels.current;
      const target = Math.min(1, (l.playing ? l.out * 4.2 : l.mic * 9));
      smooth += (target - smooth) * 0.22;
      el.current?.style.setProperty("--amp", smooth.toFixed(3));
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [levels]);

  return (
    <div className={`orb-stage orb--${mode}`} ref={el} role="img" aria-label={`Assistant is ${mode}`}>
      <div className="orb">
        <div className="orb-spin">
          {Array.from({ length: RINGS }, (_, i) => (
            <span key={i} className="ring meridian" style={{ ["--i" as string]: i }} />
          ))}
          <span className="ring equator" />
          <span className="ring tilt tilt-a" />
          <span className="ring tilt tilt-b" />
        </div>
        <div className="orb-core" />
      </div>
      <div className="orb-floor" />
    </div>
  );
}
