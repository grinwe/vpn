// Tiny hash-based router. We avoid react-router because the WebApp has
// at most a half-dozen screens and a bundled router is more code than
// the entire screen list. Hash routing also dodges the need for nginx
// to handle SPA fallbacks beyond the existing /app/index.html rule.

import { useEffect, useState } from "react";

export type Route =
  | { name: "home" }
  | { name: "plans" }
  | { name: "history" }
  | { name: "checkout"; invoiceId: number };

function parseHash(): Route {
  const raw = window.location.hash.replace(/^#/, "");
  if (raw === "" || raw === "/") return { name: "home" };
  if (raw === "plans" || raw === "/plans") return { name: "plans" };
  if (raw === "history" || raw === "/history") return { name: "history" };
  const m = raw.match(/^\/?checkout\/(\d+)$/);
  if (m) return { name: "checkout", invoiceId: Number(m[1]) };
  return { name: "home" };
}

export function navigate(route: Route): void {
  const path =
    route.name === "home"
      ? "/"
      : route.name === "plans"
        ? "/plans"
        : route.name === "history"
          ? "/history"
          : `/checkout/${route.invoiceId}`;
  window.location.hash = path;
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(parseHash);
  useEffect(() => {
    const onChange = () => setRoute(parseHash());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}
