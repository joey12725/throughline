import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Bundled variable fonts (no network): interface, long-read, mono.
import "@fontsource-variable/instrument-sans";
import "@fontsource-variable/newsreader";
import "@fontsource-variable/jetbrains-mono";

import "./ui/tokens.css";
import { App } from "./app/App";

const root = document.getElementById("root");
if (!root) throw new Error("#root not found");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
