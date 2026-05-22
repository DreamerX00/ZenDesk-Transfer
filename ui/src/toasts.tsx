import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { PropsWithChildren } from "react";
import { playForTone } from "./sound";

type ToastTone = "info" | "success" | "warning" | "danger";

interface ToastInput {
  title: string;
  message?: string;
  tone?: ToastTone;
  durationMs?: number;
}

interface ToastRecord extends ToastInput {
  id: string;
  tone: ToastTone;
}

interface ToastContextValue {
  notify: (toast: ToastInput) => void;
}

const ToastContext = createContext<ToastContextValue>({
  notify: () => undefined,
});

export function ToastProvider({ children }: PropsWithChildren) {
  const [toasts, setToasts] = useState<ToastRecord[]>([]);
  const timersRef = useRef<Map<string, number>>(new Map());

  const dismiss = useCallback((id: string) => {
    const timer = timersRef.current.get(id);
    if (typeof timer === "number") {
      window.clearTimeout(timer);
      timersRef.current.delete(id);
    }
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const notify = useCallback((toast: ToastInput) => {
    const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const tone = toast.tone ?? "info";
    setToasts((current) => [...current.slice(-3), { ...toast, id, tone }]);

    const timer = window.setTimeout(() => dismiss(id), toast.durationMs ?? 3600);
    timersRef.current.set(id, timer);

    // Audio feedback: success.mp3 for happy paths, error.mp3 for
    // failures/warnings. Info toasts stay silent (see playForTone).
    playForTone(tone);
  }, [dismiss]);

  useEffect(() => {
    return () => {
      for (const timer of timersRef.current.values()) {
        window.clearTimeout(timer);
      }
      timersRef.current.clear();
    };
  }, []);

  const value = useMemo(() => ({ notify }), [notify]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="zd-toast-stack" aria-live="polite" aria-atomic="true">
        {toasts.map((toast) => (
          <article key={toast.id} className={`zd-toast zd-toast--${toast.tone}`}>
            <header>
              <strong>{toast.title}</strong>
              <button
                aria-label="Dismiss notification"
                onClick={() => dismiss(toast.id)}
                type="button"
              >
                x
              </button>
            </header>
            {toast.message ? <p>{toast.message}</p> : null}
          </article>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue["notify"] {
  return useContext(ToastContext).notify;
}
