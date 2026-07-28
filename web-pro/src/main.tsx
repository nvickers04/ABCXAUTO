import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { AppShell } from "@/components/layout/AppShell";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <div className="h-screen w-screen overflow-hidden">
      <AppShell />
    </div>
  </StrictMode>,
);
