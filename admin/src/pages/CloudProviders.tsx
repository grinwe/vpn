import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError } from "../api";

interface CloudProviderOut {
  id: number;
  name: string;
  kind: string;
  default_image: string | null;
  default_region: string | null;
  default_plan: string | null;
  ssh_key_ids: string[] | null;
  is_active: boolean;
  created_at: string;
}

const KINDS = ["hetzner", "vultr", "digitalocean", "aeza", "manual"] as const;

export default function CloudProviders() {
  const qc = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);

  const { data, isLoading, error } = useQuery<CloudProviderOut[]>({
    queryKey: ["cloud-providers"],
    queryFn: () => api.get("/cloud/providers"),
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => api.del(`/cloud/providers/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["cloud-providers"] }),
    onError: (e: Error) => alert(`Ошибка: ${e.message}`),
  });

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-bold">Cloud Providers</h1>
        <button
          onClick={() => { setShowForm(true); setEditId(null); }}
          className="text-sm px-3 py-1 rounded bg-green-700 hover:bg-green-600"
        >
          + Добавить
        </button>
      </div>

      {isLoading && <div className="text-slate-400">Загрузка…</div>}
      {error && <div className="text-red-400">{(error as Error).message}</div>}

      {showForm && (
        <ProviderForm
          editProvider={editId != null ? data?.find((p) => p.id === editId) : undefined}
          onDone={() => { setShowForm(false); setEditId(null); }}
        />
      )}

      {data && (
        <table className="w-full text-sm">
          <thead className="text-slate-400 border-b border-slate-700">
            <tr>
              <th className="text-left py-2 px-2">ID</th>
              <th className="text-left py-2 px-2">Name</th>
              <th className="text-left py-2 px-2">Kind</th>
              <th className="text-left py-2 px-2">Region</th>
              <th className="text-left py-2 px-2">Plan</th>
              <th className="text-left py-2 px-2">Active</th>
              <th className="text-left py-2 px-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {data.map((p) => (
              <tr key={p.id} className="border-b border-slate-800 hover:bg-slate-800/50">
                <td className="py-2 px-2">{p.id}</td>
                <td className="py-2 px-2 font-mono">{p.name}</td>
                <td className="py-2 px-2">{p.kind}</td>
                <td className="py-2 px-2 text-slate-400">{p.default_region || "—"}</td>
                <td className="py-2 px-2 text-slate-400">{p.default_plan || "—"}</td>
                <td className="py-2 px-2">{p.is_active ? "✓" : "✕"}</td>
                <td className="py-2 px-2">
                  <div className="flex gap-1">
                    <button
                      onClick={() => { setEditId(p.id); setShowForm(true); }}
                      className="text-xs px-2 py-1 rounded bg-blue-700 hover:bg-blue-600"
                    >
                      edit
                    </button>
                    <button
                      disabled={deleteMut.isPending}
                      onClick={() => {
                        if (confirm(`Удалить провайдер ${p.name}?`))
                          deleteMut.mutate(p.id);
                      }}
                      className="text-xs px-2 py-1 rounded bg-red-800 hover:bg-red-700 disabled:opacity-50"
                    >
                      delete
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ProviderForm({
  editProvider,
  onDone,
}: {
  editProvider?: CloudProviderOut;
  onDone: () => void;
}) {
  const qc = useQueryClient();
  const isEdit = !!editProvider;

  const [name, setName] = useState(editProvider?.name ?? "");
  const [kind, setKind] = useState(editProvider?.kind ?? "hetzner");
  const [apiToken, setApiToken] = useState("");
  const [defaultImage, setDefaultImage] = useState(editProvider?.default_image ?? "");
  const [defaultRegion, setDefaultRegion] = useState(editProvider?.default_region ?? "");
  const [defaultPlan, setDefaultPlan] = useState(editProvider?.default_plan ?? "");
  const [sshKeyIds, setSshKeyIds] = useState(editProvider?.ssh_key_ids?.join(", ") ?? "");
  const [isActive, setIsActive] = useState(editProvider?.is_active ?? true);
  const [err, setErr] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      isEdit
        ? api.patch(`/cloud/providers/${editProvider!.id}`, body)
        : api.post("/cloud/providers", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["cloud-providers"] });
      onDone();
    },
    onError: (e: Error) => setErr(e instanceof ApiError ? `${e.status}: ${e.message}` : e.message),
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    const body: Record<string, unknown> = {
      name,
      kind,
      default_image: defaultImage || null,
      default_region: defaultRegion || null,
      default_plan: defaultPlan || null,
      ssh_key_ids: sshKeyIds ? sshKeyIds.split(",").map((s) => s.trim()) : null,
      is_active: isActive,
    };
    if (apiToken) body.api_token = apiToken;
    mutation.mutate(body);
  }

  return (
    <form
      onSubmit={submit}
      className="mb-4 p-4 rounded border border-slate-700 bg-slate-900 grid grid-cols-2 gap-3 text-sm"
    >
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Name</span>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Kind</span>
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value)}
          disabled={isEdit}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        >
          {KINDS.map((k) => (
            <option key={k} value={k}>{k}</option>
          ))}
        </select>
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">API Token {isEdit && "(оставь пустым чтобы не менять)"}</span>
        <input
          type="password"
          value={apiToken}
          onChange={(e) => setApiToken(e.target.value)}
          placeholder={isEdit ? "••••••••" : ""}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Default Region</span>
        <input
          value={defaultRegion}
          onChange={(e) => setDefaultRegion(e.target.value)}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Default Plan</span>
        <input
          value={defaultPlan}
          onChange={(e) => setDefaultPlan(e.target.value)}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Default Image</span>
        <input
          value={defaultImage}
          onChange={(e) => setDefaultImage(e.target.value)}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">SSH Key IDs (comma-separated)</span>
        <input
          value={sshKeyIds}
          onChange={(e) => setSshKeyIds(e.target.value)}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
      </label>
      <label className="flex items-center gap-2 self-end">
        <input
          type="checkbox"
          checked={isActive}
          onChange={(e) => setIsActive(e.target.checked)}
        />
        <span className="text-slate-400">Active</span>
      </label>

      {err && <div className="col-span-2 text-red-400 text-xs">{err}</div>}
      <div className="col-span-2 flex gap-2 justify-end">
        <button type="button" onClick={onDone} className="text-xs px-3 py-1 rounded bg-slate-700">
          Отмена
        </button>
        <button
          type="submit"
          disabled={mutation.isPending}
          className="text-xs px-3 py-1 rounded bg-green-700 hover:bg-green-600 disabled:opacity-50"
        >
          {isEdit ? "Сохранить" : "Создать"}
        </button>
      </div>
    </form>
  );
}
