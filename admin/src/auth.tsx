import { createContext, useCallback, useContext, useEffect, useState, ReactNode } from "react";
import { api, getToken, setToken, ApiError } from "./api";

interface AuthCtx {
  token: string | null;
  loginState: "idle" | "checking" | "ok" | "error";
  login: (token: string) => Promise<boolean>;
  logout: () => void;
}

const Ctx = createContext<AuthCtx | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTok] = useState<string | null>(getToken());
  const [loginState, setLoginState] = useState<AuthCtx["loginState"]>(
    token ? "ok" : "idle"
  );

  // Probe the token on mount — if backend rejects, clear it so the user
  // lands on the login screen instead of a half-working UI.
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    (async () => {
      try {
        await api.get("/users?limit=1");
        if (!cancelled) setLoginState("ok");
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
          setToken(null);
          setTok(null);
          setLoginState("idle");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const login = useCallback(async (candidate: string) => {
    setLoginState("checking");
    setToken(candidate);
    try {
      await api.get("/users?limit=1");
      setTok(candidate);
      setLoginState("ok");
      return true;
    } catch {
      setToken(null);
      setTok(null);
      setLoginState("error");
      return false;
    }
  }, []);

  const logout = useCallback(() => {
    setToken(null);
    setTok(null);
    setLoginState("idle");
  }, []);

  return (
    <Ctx.Provider value={{ token, loginState, login, logout }}>{children}</Ctx.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
