// Mounts the Agentation visual-feedback toolbar onto the dashboard.
// Vite resolves these bare imports from frontend/node_modules.
import React from "react";
import { createRoot } from "react-dom/client";
import { Agentation } from "agentation";

// Host container for the toolbar portal.
const host = document.createElement("div");
host.id = "agentation-root";
document.body.appendChild(host);

/**
 * Agentation renders a floating toolbar. Click it to enter annotation mode,
 * click any element on the page (phone bubbles, trace rows, metrics...),
 * leave a comment, then Copy or "Send to Agent".
 *
 * Feedback is persisted to localStorage per-path. onSubmit/onCopy also log the
 * structured markdown to the console so you can paste it straight to an agent.
 */
function Toolbar() {
  return React.createElement(Agentation, {
    appName: "ALDO Fit Gateway — Demo Dashboard",
    accentColor: "#e4572e",
    copyToClipboard: true,
    enableKeyboardShortcuts: true,
    onSubmit: (markdown, annotations) => {
      console.log("[agentation] send-to-agent payload:\n", markdown);
      console.log("[agentation] annotations:", annotations);
    },
    onCopy: (output) => {
      console.log("[agentation] copied feedback:\n", output);
    },
  });
}

createRoot(host).render(React.createElement(Toolbar));
console.log("%c[agentation] visual feedback toolbar loaded", "color:#e4572e;font-weight:600");
