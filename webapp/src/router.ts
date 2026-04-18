// Tiny hash-based router. We avoid react-router because the WebApp has
// at most a half-dozen screens and a bundled router is more code than
// the entire screen list. Hash routing also dodges the need for nginx
// to handle SPA fallbacks beyond the existing /app/index.html rule.

import { useEffect, useState } from "react";

export type Route =
  | { name: "home" }
  | { name: "plans"; subscriptionId?: number }
  | { name: "history" }
  | { name: "help" }
  | { name: "checkout"; invoiceId: number };

function parseHash(): Route {
  const raw = window.location.hash.replace(/^#/, "");
  if (raw === "" || raw === "/") return { name: "home" };
  if (raw === "plans" || raw === "/plans") return { name: "plans" };
  const planChange = raw.match(/^\/?plans\/change\/(\d+)$/);
  if (planChange) return { name: "plans", subscriptionId: Number(planChange[1]) };
  if (raw === "history" || raw === "/history") return { name: "history" };
  if (raw === "help" || raw === "/help") return { name: "help" };
  const m = raw.match(/^\/?checkout\/(\d+)$/);
  if (m) return { name: "checkout", invoiceId: Number(m[1]) };
  return { name: "home" };
}

export function navigate(route: Route): void {
  const path =
    route.name === "home"
      ? "/"
      : route.name === "plans"
        ? route.subscriptionId
          ? `/plans/change/${route.subscriptionId}`
          : "/plans"
        : route.name === "history"
          ? "/history"
          : route.name === "help"
            ? "/help"
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
