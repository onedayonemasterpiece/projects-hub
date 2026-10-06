import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";
import "./collaboration.css";
import "./collaboration-questions.css";
import "./inline-board.css";

if ("serviceWorker" in navigator && import.meta.env.PROD) {
  let reloadingForWorker = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (reloadingForWorker) return;
    reloadingForWorker = true;
    window.location.reload();
  });
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" })
      .then(registration => registration.update())
      .catch(() => {});
  });
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
