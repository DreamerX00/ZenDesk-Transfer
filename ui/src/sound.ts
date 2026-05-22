/**
 * Audio feedback for the wizard.
 *
 * The three clips (`click.mp3`, `success.mp3`, `error.mp3`) live next
 * to `iframe.html` in `ui/assets/`. Vite copies them into the build,
 * and the FastAPI catch-all at `/` serves them with `audio/mpeg`. The
 * paths below use `import.meta.env.BASE_URL` so the same code works
 * regardless of where the bundle is mounted.
 *
 * Browsers block `Audio.play()` until the page has received a user
 * gesture, so we keep the clips silent until the first interaction;
 * after that, every notify/click can play freely. We also keep a tiny
 * pool of cloned elements per clip so two rapid `playClick()` calls
 * don't stomp on each other.
 */

type ClipName = "click" | "success" | "error";

const FILES: Record<ClipName, string> = {
  click: "click.mp3",
  success: "success.mp3",
  error: "error.mp3",
};

const VOLUMES: Record<ClipName, number> = {
  click: 0.35, // soft — fires on every button press
  success: 0.7,
  error: 0.7,
};

const POOL_SIZE: Record<ClipName, number> = {
  click: 4,
  success: 2,
  error: 2,
};

interface Pool {
  elements: HTMLAudioElement[];
  cursor: number;
}

const pools: Partial<Record<ClipName, Pool>> = {};
let unlocked = false;
let userMuted = false;

function buildPool(name: ClipName): Pool {
  const size = POOL_SIZE[name];
  const elements: HTMLAudioElement[] = [];
  for (let i = 0; i < size; i += 1) {
    const el = new Audio(new URL(FILES[name], document.baseURI).toString());
    el.preload = "auto";
    el.volume = VOLUMES[name];
    elements.push(el);
  }
  return { elements, cursor: 0 };
}

function getPool(name: ClipName): Pool {
  let pool = pools[name];
  if (!pool) {
    pool = buildPool(name);
    pools[name] = pool;
  }
  return pool;
}

function play(name: ClipName): void {
  if (typeof window === "undefined") return;
  if (userMuted) return;
  // No point firing audio before a user gesture — the browser will
  // reject it and we'd spam the console with autoplay warnings.
  if (!unlocked && name !== "click") return;
  try {
    const pool = getPool(name);
    const el = pool.elements[pool.cursor];
    pool.cursor = (pool.cursor + 1) % pool.elements.length;
    el.currentTime = 0;
    const result = el.play();
    if (result && typeof result.catch === "function") {
      result.catch(() => undefined); // autoplay block — silent fallback
    }
  } catch {
    // Audio is best-effort; never let a sound failure break the UI.
  }
}

export function playClick(): void {
  unlocked = true;
  play("click");
}

export function playSuccess(): void {
  play("success");
}

export function playError(): void {
  play("error");
}

/** Toggle off all sounds. Surfaced via the mute button. */
export function setMuted(muted: boolean): void {
  userMuted = muted;
  if (muted) {
    // Stop anything mid-flight.
    for (const pool of Object.values(pools)) {
      if (!pool) continue;
      for (const el of pool.elements) {
        el.pause();
        el.currentTime = 0;
      }
    }
  }
  try {
    window.localStorage.setItem("zdx_sound_muted", muted ? "1" : "0");
  } catch {
    // Storage may be unavailable inside the Zendesk iframe — ignore.
  }
}

export function isMuted(): boolean {
  return userMuted;
}

/**
 * Wire up a global click listener so every native button/anchor/role
 * element in the app triggers `playClick()` automatically. This keeps
 * the per-step JSX untouched.
 */
export function installClickListener(): void {
  if (typeof window === "undefined") return;
  try {
    const stored = window.localStorage.getItem("zdx_sound_muted");
    if (stored === "1") userMuted = true;
  } catch {
    // ignore
  }

  // Pre-warm the pools after first user interaction so the first
  // success/error chord isn't delayed by a decode pause.
  const warm = () => {
    if (unlocked) return;
    unlocked = true;
    getPool("click");
    getPool("success");
    getPool("error");
  };

  window.addEventListener("pointerdown", warm, { once: true, capture: true });
  window.addEventListener("keydown", warm, { once: true, capture: true });

  document.addEventListener(
    "click",
    (event) => {
      const target = event.target as Element | null;
      if (!target || !target.closest) return;
      // Anything the user perceives as a button: real <button>, links,
      // radios/checkboxes, file inputs, and ARIA-tabbed step buttons.
      const hit = target.closest(
        'button, a[href], [role="button"], [role="tab"], input[type="radio"], input[type="checkbox"], input[type="file"], label.zd-field, label.zd-checkbox',
      );
      if (!hit) return;
      if ((hit as HTMLButtonElement).disabled) return;
      playClick();
    },
    { capture: true },
  );
}

/** Used by the toast layer to map tone → clip. */
export function playForTone(tone: "info" | "success" | "warning" | "danger"): void {
  if (tone === "success") {
    playSuccess();
  } else if (tone === "danger" || tone === "warning") {
    playError();
  }
  // "info" stays silent on purpose — the click that opened it was
  // already announced, and idle informational toasts shouldn't beep.
}
