import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import StageApp from "./StageApp";
import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "./styles/base.css";
import "./styles/shell.css";
import "./styles/components.css";
import "./styles/phone.css";
import "./styles/picker.css";
import "./styles/markdown.css";
import "./styles/chat.css";
import "./styles/cards.css";
import "./styles/sheets.css";
import "./styles/trace.css";
import "./styles/cases.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { refetchOnWindowFocus: false, staleTime: 0 },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <StageApp />
    </QueryClientProvider>
  </StrictMode>,
);
