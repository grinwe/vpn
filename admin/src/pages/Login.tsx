import { useState } from "react";
import { useAuth } from "../auth";

export default function Login() {
  const { login, loginState } = useAuth();
  const [value, setValue] = useState("");

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!value.trim()) return;
    await login(value.trim());
  };

  return (
    <div className="min-h-full flex items-center justify-center">
      <form
        onSubmit={onSubmit}
        className="bg-slate-800 p-6 rounded-lg shadow w-full max-w-sm space-y-4"
      >
        <h1 className="text-xl font-semibold">VPN Admin</h1>
        <label className="block text-sm">
          Admin token
          <input
            type="password"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="mt-1 w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 focus:outline-none focus:border-slate-500"
            autoFocus
          />
        </label>
        {loginState === "error" && (
          <p className="text-sm text-red-400">Неверный токен</p>
        )}
        <button
          type="submit"
          disabled={loginState === "checking"}
          className="w-full py-2 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50"
        >
          {loginState === "checking" ? "..." : "Войти"}
        </button>
      </form>
    </div>
  );
}
