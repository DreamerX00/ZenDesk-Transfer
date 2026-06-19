import { useCallback, useEffect, useRef, useState } from "react";

type Theme = "light" | "dark";

const STORAGE_KEY = "zd-theme";

const STARS = [
  { x: 12, y: 18, delay: 0, dur: 2.4 },
  { x: 82, y: 14, delay: 0.35, dur: 3.1 },
  { x: 24, y: 68, delay: 0.6, dur: 2.7 },
  { x: 72, y: 58, delay: 0.9, dur: 3.3 },
  { x: 48, y: 22, delay: 1.15, dur: 2.5 },
  { x: 90, y: 42, delay: 0.45, dur: 3.6 },
  { x: 8, y: 48, delay: 0.7, dur: 2.9 },
  { x: 58, y: 78, delay: 1.0, dur: 3.0 },
  { x: 34, y: 38, delay: 0.2, dur: 2.6 },
  { x: 86, y: 72, delay: 0.55, dur: 3.4 },
  { x: 18, y: 82, delay: 0.8, dur: 2.8 },
  { x: 66, y: 32, delay: 1.3, dur: 3.2 },
];

function getInitialTheme(): Theme {
  if (typeof window === "undefined") return "light";
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === "dark" || stored === "light") return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(getInitialTheme);
  const btnRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  const toggle = useCallback(
    (e: React.MouseEvent<HTMLButtonElement>) => {
      const next: Theme = theme === "dark" ? "light" : "dark";
      const x = e.clientX;
      const y = e.clientY;
      const endRadius = Math.hypot(
        Math.max(x, window.innerWidth - x),
        Math.max(y, window.innerHeight - y),
      );

      const apply = () => {
        setTheme(next);
        localStorage.setItem(STORAGE_KEY, next);
        document.documentElement.setAttribute("data-theme", next);
      };

      const reduceMotion = window.matchMedia(
        "(prefers-reduced-motion: reduce)",
      ).matches;

      const vt = (
        document as Document & {
          startViewTransition?: (cb: () => void) => {
            ready: Promise<void>;
          };
        }
      ).startViewTransition;

      if (vt && !reduceMotion) {
        const transition = vt.call(document, apply);
        transition.ready
          .then(() => {
            document.documentElement.animate(
              {
                clipPath: [
                  `circle(0px at ${x}px ${y}px)`,
                  `circle(${endRadius}px at ${x}px ${y}px)`,
                ],
              },
              {
                duration: 720,
                easing: "cubic-bezier(0.25, 0.1, 0.25, 1)",
                pseudoElement: "::view-transition-new(root)",
              },
            );
          })
          .catch(() => {});
      } else {
        apply();
      }
    },
    [theme],
  );

  const handleMouseMove = useCallback(
    (e: React.MouseEvent<HTMLButtonElement>) => {
      const btn = btnRef.current;
      if (!btn) return;
      const rect = btn.getBoundingClientRect();
      const px = (e.clientX - rect.left) / rect.width - 0.5;
      const py = (e.clientY - rect.top) / rect.height - 0.5;
      btn.style.setProperty("--tilt-x", `${py * -22}deg`);
      btn.style.setProperty("--tilt-y", `${px * 22}deg`);
      btn.style.setProperty("--glow-x", `${(px + 0.5) * 100}%`);
      btn.style.setProperty("--glow-y", `${(py + 0.5) * 100}%`);
    },
    [],
  );

  const handleMouseLeave = useCallback(() => {
    const btn = btnRef.current;
    if (!btn) return;
    btn.style.setProperty("--tilt-x", "0deg");
    btn.style.setProperty("--tilt-y", "0deg");
  }, []);

  const isDark = theme === "dark";

  return (
    <button
      ref={btnRef}
      type="button"
      className="zd-theme-toggle"
      aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
      aria-pressed={isDark}
      title={isDark ? "Switch to light theme" : "Switch to dark theme"}
      onClick={toggle}
      onMouseMove={handleMouseMove}
      onMouseLeave={handleMouseLeave}
    >
      <span className="zd-theme-sky" />
      <span className="zd-theme-cloud zd-theme-cloud--1" />
      <span className="zd-theme-cloud zd-theme-cloud--2" />

      <span className="zd-theme-celestial zd-theme-sun">
        <span className="zd-theme-rays" />
        <span className="zd-theme-sun-body" />
      </span>

      <span className="zd-theme-celestial zd-theme-moon">
        <span className="zd-theme-moon-body" />
        <span className="zd-theme-crater zd-theme-crater--1" />
        <span className="zd-theme-crater zd-theme-crater--2" />
        <span className="zd-theme-crater zd-theme-crater--3" />
      </span>

      <span className="zd-theme-stars">
        {STARS.map((s, i) => (
          <span
            key={i}
            className="zd-theme-star"
            style={{
              left: `${s.x}%`,
              top: `${s.y}%`,
              animationDelay: `${s.delay}s`,
              animationDuration: `${s.dur}s`,
            }}
          />
        ))}
      </span>

      <span className="zd-theme-shooting-star" />
      <span className="zd-theme-ring" />
    </button>
  );
}
