import { useEffect, useState } from "react";
import { Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useAuth } from "./auth";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import Users from "./pages/Users";
import Invoices from "./pages/Invoices";
import Nodes from "./pages/Nodes";
import Versions from "./pages/Versions";
import Plans from "./pages/Plans";
import ApiTokens from "./pages/ApiTokens";
import Tasks from "./pages/Tasks";
import AuditLogs from "./pages/AuditLogs";
import CloudProviders from "./pages/CloudProviders";
import Exits from "./pages/Exits";
import HealthPings from "./pages/HealthPings";
import Broadcasts from "./pages/Broadcasts";
import OperatorMatrix from "./pages/OperatorMatrix";
import AdLinks from "./pages/AdLinks";

const NAV_ITEMS: { to: string; label: string; end?: boolean }[] = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/users", label: "Users" },
  { to: "/invoices", label: "Invoices" },
  { to: "/plans", label: "Plans" },
  { to: "/nodes", label: "Nodes" },
  { to: "/versions", label: "Версии" },
  { to: "/exits", label: "Exits" },
  { to: "/tasks", label: "Tasks" },
  { to: "/health-pings", label: "Health" },
  { to: "/operators", label: "Operators" },
  { to: "/broadcasts", label: "Broadcasts" },
  { to: "/ad-links", label: "Реклама" },
  { to: "/tokens", label: "API tokens" },
  { to: "/cloud", label: "Cloud" },
  { to: "/audit", label: "Audit" },
];

function Layout({ children }: { children: React.ReactNode }) {
  const { logout } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const location = useLocation();
  const linkCls = ({ isActive }: { isActive: boolean }) =>
    `px-3 py-2 rounded whitespace-nowrap ${
      isActive ? "bg-slate-700" : "hover:bg-slate-800"
    }`;
  const current =
    NAV_ITEMS.find((i) => (i.end ? location.pathname === i.to : location.pathname.startsWith(i.to)))
      ?.label ?? "Admin";

  // Меню закрываем при переходе — иначе после тапа по ссылке панель остаётся
  // висеть поверх страницы, на которую ты только что перешёл.
  useEffect(() => setMenuOpen(false), [location.pathname]);

  return (
    <div className="min-h-full flex flex-col">
      <header className="bg-slate-950 border-b border-slate-800 sticky top-0 z-30">
        {/* Мобильная шапка: имя раздела + бургер. 14 ссылок в одну строку на
            телефоне превращались в кашу, которая ещё и распирала вьюпорт. */}
        <div className="flex items-center gap-2 px-3 py-2 md:hidden">
          <button
            onClick={() => setMenuOpen((v) => !v)}
            aria-label="Меню"
            aria-expanded={menuOpen}
            className="px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-lg leading-none"
          >
            {menuOpen ? "✕" : "☰"}
          </button>
          <span className="font-semibold">{current}</span>
          <button
            onClick={logout}
            className="ml-auto text-sm px-3 py-1.5 rounded bg-slate-800 hover:bg-slate-700"
          >
            Выйти
          </button>
        </div>
        {menuOpen && (
          <nav className="md:hidden grid grid-cols-2 gap-1 px-3 pb-3 text-sm border-t border-slate-800 pt-2">
            {NAV_ITEMS.map((i) => (
              <NavLink key={i.to} to={i.to} end={i.end} className={linkCls}>
                {i.label}
              </NavLink>
            ))}
          </nav>
        )}

        {/* Десктоп: прежняя строка, но со скроллом — на 1280px 14 пунктов
            вытесняли кнопку выхода за край экрана. */}
        <div className="hidden md:flex items-center gap-2 px-4 py-2">
          <span className="font-semibold mr-4 shrink-0">VPN Admin</span>
          <nav className="flex gap-1 text-sm overflow-x-auto">
          <NavLink to="/" end className={linkCls}>Dashboard</NavLink>
          <NavLink to="/users" className={linkCls}>Users</NavLink>
          <NavLink to="/invoices" className={linkCls}>Invoices</NavLink>
          <NavLink to="/plans" className={linkCls}>Plans</NavLink>
          <NavLink to="/nodes" className={linkCls}>Nodes</NavLink>
          <NavLink to="/versions" className={linkCls}>Версии</NavLink>
          <NavLink to="/exits" className={linkCls}>Exits</NavLink>
          <NavLink to="/tasks" className={linkCls}>Tasks</NavLink>
          <NavLink to="/health-pings" className={linkCls}>Health</NavLink>
          <NavLink to="/operators" className={linkCls}>Operators</NavLink>
          <NavLink to="/broadcasts" className={linkCls}>Broadcasts</NavLink>
          <NavLink to="/ad-links" className={linkCls}>Реклама</NavLink>
          <NavLink to="/tokens" className={linkCls}>API tokens</NavLink>
          <NavLink to="/cloud" className={linkCls}>Cloud</NavLink>
          <NavLink to="/audit" className={linkCls}>Audit</NavLink>
          </nav>
          <button
            onClick={logout}
            className="ml-auto shrink-0 text-sm px-3 py-2 rounded bg-slate-800 hover:bg-slate-700"
          >
            Выйти
          </button>
        </div>
      </header>
      {/* min-w-0 обязателен: без него широкие таблицы внутри flex-колонки
          растягивают контейнер и ломают вёрстку всей страницы. */}
      <main className="flex-1 min-w-0 p-3 md:p-6">{children}</main>
    </div>
  );
}

function Protected({ children }: { children: React.ReactNode }) {
  const { loginState } = useAuth();
  if (loginState !== "ok") return <Navigate to="/login" replace />;
  return <Layout>{children}</Layout>;
}

export default function App() {
  const { loginState } = useAuth();
  return (
    <Routes>
      <Route
        path="/login"
        element={loginState === "ok" ? <Navigate to="/" replace /> : <Login />}
      />
      <Route path="/" element={<Protected><Dashboard /></Protected>} />
      <Route path="/users" element={<Protected><Users /></Protected>} />
      <Route path="/invoices" element={<Protected><Invoices /></Protected>} />
      <Route path="/plans" element={<Protected><Plans /></Protected>} />
      <Route path="/nodes" element={<Protected><Nodes /></Protected>} />
      <Route path="/versions" element={<Protected><Versions /></Protected>} />
      <Route path="/exits" element={<Protected><Exits /></Protected>} />
      <Route path="/tasks" element={<Protected><Tasks /></Protected>} />
      <Route path="/tokens" element={<Protected><ApiTokens /></Protected>} />
      <Route path="/cloud" element={<Protected><CloudProviders /></Protected>} />
      <Route path="/audit" element={<Protected><AuditLogs /></Protected>} />
      <Route
        path="/health-pings"
        element={<Protected><HealthPings /></Protected>}
      />
      <Route
        path="/broadcasts"
        element={<Protected><Broadcasts /></Protected>}
      />
      <Route
        path="/operators"
        element={<Protected><OperatorMatrix /></Protected>}
      />
      <Route path="/ad-links" element={<Protected><AdLinks /></Protected>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
