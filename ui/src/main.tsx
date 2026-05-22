import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ThemeProvider, DEFAULT_THEME } from "@zendeskgarden/react-theming";
import App from "./App";
import { ToastProvider } from "./toasts";
import { installClickListener } from "./sound";
import "./styles.css";

const root = document.getElementById("root");
if (!root) {
  throw new Error("missing #root element in iframe.html");
}

// One delegated listener handles every button/anchor click — keeps the
// per-component JSX clean and survives DOM swaps inside React.
installClickListener();

createRoot(root).render(
  <StrictMode>
    <ThemeProvider theme={DEFAULT_THEME}>
      <ToastProvider>
        <App />
      </ToastProvider>
    </ThemeProvider>
  </StrictMode>,
);
