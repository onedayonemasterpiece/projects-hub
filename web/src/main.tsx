import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import GuestBoard from "./GuestBoard";
import "./styles.css";

const guestMode = window.location.pathname === "/guest/board";

if (!guestMode && "serviceWorker" in navigator && import.meta.env.PROD) {
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
    {guestMode ? <GuestBoard /> : <App />}
  </StrictMode>,
);
