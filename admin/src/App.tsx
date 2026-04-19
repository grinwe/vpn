import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import Users from "./pages/Users";
import Invoices from "./pages/Invoices";
import Nodes from "./pages/Nodes";
import Plans from "./pages/Plans";
import ApiTokens from "./pages/ApiTokens";
import Tasks from "./pages/Tasks";
import AuditLogs from "./pages/AuditLogs";
import CloudProviders from "./pages/CloudProviders";
import Exits from "./pages/Exits";
import HealthPings from "./pages/HealthPings";
import Broadcasts from "./pages/Broadcasts";

function Layout({ children }: { children: React.ReactNode }) {
  const { logout } = useAuth();
  const linkCls = ({ isActive }: { isActive: boolean }) =>
    `px-3 py-2 rounded ${isActive ? "bg-slate-700" : "hover:bg-slate-800"}`;
  return (
    <div className="min-h-full flex flex-col">
      <header className="bg-slate-950 border-b border-slate-800 px-4 py-2 flex items-center gap-2">
        <span className="font-semibold mr-4">VPN Admin</span>
        <nav className="flex gap-1 text-sm">
          <NavLink to="/" end className={linkCls}>Dashboard</NavLink>
          <NavLink to="/users" className={linkCls}>Users</NavLink>
          <NavLink to="/invoices" className={linkCls}>Invoices</NavLink>
          <NavLink to="/plans" className={linkCls}>Plans</NavLink>
          <NavLink to="/nodes" className={linkCls}>Nodes</NavLink>
          <NavLink to="/exits" className={linkCls}>Exits</NavLink>
          <NavLink to="/tasks" className={linkCls}>Tasks</NavLink>
          <NavLink to="/health-pings" className={linkCls}>Health</NavLink>
          <NavLink to="/broadcasts" className={linkCls}>Broadcasts</NavLink>
          <NavLink to="/tokens" className={linkCls}>API tokens</NavLink>
          <NavLink to="/cloud" className={linkCls}>Cloud</NavLink>
          <NavLink to="/audit" className={linkCls}>Audit</NavLink>
        </nav>
        <button
          onClick={logout}
          className="ml-auto text-sm px-3 py-2 rounded bg-slate-800 hover:bg-slate-700"
        >
          Logout
        </button>
      </header>
      <main className="flex-1 p-6">{children}</main>
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
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
