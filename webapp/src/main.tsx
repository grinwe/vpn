import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";
import { applyTheme, getTg } from "./telegram";

const tg = getTg();
if (tg) {
  applyTheme(tg);
  tg.expand();
  tg.ready();
  tg.onEvent("themeChanged", () => applyTheme(tg));
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
