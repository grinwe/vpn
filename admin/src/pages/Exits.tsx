import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError } from "../api";

interface WGExitNodeOut {
  id: number;
  name: string;
  region: string;
  host: string;
  ssh_port: number;
  wg_port: number;
  wg_address_v4: string;
  wg_public_key: string | null;
  has_private_key: boolean;
  provider_id: number | null;
  provider_external_id: string | null;
  provider_region: string | null;
  status: string;
  is_active: boolean;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

interface KeygenOut {
  id: number;
  wg_public_key: string;
}

interface CloudProviderOut {
  id: number;
  name: string;
  kind: string;
}

const STATUSES = ["registering", "active", "error", "disabled"] as const;

export default function Exits() {
  const qc = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);

  const { data, isLoading, error } = useQuery<WGExitNodeOut[]>({
    queryKey: ["wg-exits"],
    queryFn: () => api.get("/exits"),
  });

  const providers = useQuery<CloudProviderOut[]>({
    queryKey: ["cloud-providers"],
    queryFn: () => api.get("/cloud/providers"),
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => api.del(`/exits/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["wg-exits"] }),
    onError: (e: Error) => alert(`Ошибка: ${e.message}`),
  });

  const keygenMut = useMutation({
    mutationFn: (id: number) => api.post<KeygenOut>(`/exits/${id}/keygen`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["wg-exits"] }),
    onError: (e: Error) => alert(`Ошибка: ${e.message}`),
  });

  const providerLabel = (pid: number | null) => {
    if (pid == null) return "—";
    const p = providers.data?.find((x) => x.id === pid);
    return p ? `${p.name} (${p.kind})` : `#${pid}`;
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-bold">WG Exit Nodes</h1>
        <button
          onClick={() => { setShowForm(true); setEditId(null); }}
          className="text-sm px-3 py-1 rounded bg-green-700 hover:bg-green-600"
        >
          + Добавить
        </button>
      </div>

      <p className="text-xs text-slate-400 mb-4">
        Foreign exit nodes — RU relay jump nodes тоннелируют сюда через WireGuard.
        См. <code>docs/RELAY_ROADMAP.md</code>.
      </p>

      {isLoading && <div className="text-slate-400">Загрузка…</div>}
      {error && <div className="text-red-400">{(error as Error).message}</div>}

      {showForm && (
        <ExitForm
          editExit={editId != null ? data?.find((e) => e.id === editId) : undefined}
          providers={providers.data ?? []}
          onDone={() => { setShowForm(false); setEditId(null); }}
        />
      )}

      {data && (
        <table className="w-full text-sm">
          <thead className="text-slate-400 border-b border-slate-700">
            <tr>
              <th className="text-left py-2 px-2">ID</th>
              <th className="text-left py-2 px-2">Name</th>
              <th className="text-left py-2 px-2">Region</th>
              <th className="text-left py-2 px-2">Host</th>
              <th className="text-left py-2 px-2">WG port</th>
              <th className="text-left py-2 px-2">WG addr</th>
              <th className="text-left py-2 px-2">Public key</th>
              <th className="text-left py-2 px-2">Provider</th>
              <th className="text-left py-2 px-2">Status</th>
              <th className="text-left py-2 px-2">Active</th>
              <th className="text-left py-2 px-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {data.map((e) => (
              <tr key={e.id} className="border-b border-slate-800 hover:bg-slate-800/50">
                <td className="py-2 px-2">{e.id}</td>
                <td className="py-2 px-2 font-mono">{e.name}</td>
                <td className="py-2 px-2">{e.region}</td>
                <td className="py-2 px-2 font-mono text-slate-300">{e.host}</td>
                <td className="py-2 px-2">{e.wg_port}</td>
                <td className="py-2 px-2 font-mono text-slate-400">{e.wg_address_v4}</td>
                <td className="py-2 px-2 font-mono text-xs">
                  {e.wg_public_key ? (
                    <span title={e.wg_public_key}>{e.wg_public_key.slice(0, 12)}…</span>
                  ) : (
                    <span className="text-yellow-500">—</span>
                  )}
                  {!e.has_private_key && e.wg_public_key && (
                    <span className="ml-2 text-yellow-500" title="private key missing">⚠</span>
                  )}
                </td>
                <td className="py-2 px-2 text-slate-400">{providerLabel(e.provider_id)}</td>
                <td className="py-2 px-2">{e.status}</td>
                <td className="py-2 px-2">{e.is_active ? "✓" : "✕"}</td>
                <td className="py-2 px-2">
                  <div className="flex gap-1">
                    <button
                      onClick={() => { setEditId(e.id); setShowForm(true); }}
                      className="text-xs px-2 py-1 rounded bg-blue-700 hover:bg-blue-600"
                    >
                      edit
                    </button>
                    <button
                      disabled={keygenMut.isPending}
                      onClick={() => {
                        const msg = e.has_private_key
                          ? `Сгенерировать новый ключ для ${e.name}? Текущий будет перезаписан.`
                          : `Сгенерировать ключ для ${e.name}?`;
                        if (confirm(msg)) keygenMut.mutate(e.id);
                      }}
                      className="text-xs px-2 py-1 rounded bg-purple-700 hover:bg-purple-600 disabled:opacity-50"
                    >
                      keygen
                    </button>
                    <button
                      disabled={deleteMut.isPending}
                      onClick={() => {
                        if (confirm(`Удалить exit ${e.name}?`))
                          deleteMut.mutate(e.id);
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

function ExitForm({
  editExit,
  providers,
  onDone,
}: {
  editExit?: WGExitNodeOut;
  providers: CloudProviderOut[];
  onDone: () => void;
}) {
  const qc = useQueryClient();
  const isEdit = !!editExit;

  const [name, setName] = useState(editExit?.name ?? "");
  const [region, setRegion] = useState(editExit?.region ?? "");
  const [host, setHost] = useState(editExit?.host ?? "");
  const [sshPort, setSshPort] = useState(editExit?.ssh_port ?? 22);
  const [wgPort, setWgPort] = useState(editExit?.wg_port ?? 51820);
  const [wgAddress, setWgAddress] = useState(editExit?.wg_address_v4 ?? "10.77.0.1/24");
  const [wgPublic, setWgPublic] = useState(editExit?.wg_public_key ?? "");
  const [wgPrivate, setWgPrivate] = useState("");
  const [providerId, setProviderId] = useState<string>(
    editExit?.provider_id != null ? String(editExit.provider_id) : ""
  );
  const [providerExternalId, setProviderExternalId] = useState(editExit?.provider_external_id ?? "");
  const [providerRegion, setProviderRegion] = useState(editExit?.provider_region ?? "");
  const [status, setStatus] = useState(editExit?.status ?? "registering");
  const [isActive, setIsActive] = useState(editExit?.is_active ?? true);
  const [notes, setNotes] = useState(editExit?.notes ?? "");
  const [err, setErr] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      isEdit
        ? api.patch(`/exits/${editExit!.id}`, body)
        : api.post("/exits", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["wg-exits"] });
      onDone();
    },
    onError: (e: Error) => setErr(e instanceof ApiError ? `${e.status}: ${e.message}` : e.message),
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    const body: Record<string, unknown> = {
      name,
      region,
      host,
      ssh_port: Number(sshPort),
      wg_port: Number(wgPort),
      wg_address_v4: wgAddress,
      provider_id: providerId ? Number(providerId) : null,
      provider_external_id: providerExternalId || null,
      provider_region: providerRegion || null,
      is_active: isActive,
      notes: notes || null,
    };
    if (wgPublic) body.wg_public_key = wgPublic;
    if (wgPrivate) body.wg_private_key = wgPrivate;
    if (isEdit) body.status = status;
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
        <span className="text-slate-400 mb-1">Region</span>
        <input
          value={region}
          onChange={(e) => setRegion(e.target.value)}
          required
          placeholder="de, nl, fi, ..."
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col col-span-2">
        <span className="text-slate-400 mb-1">Host (IP or FQDN)</span>
        <input
          value={host}
          onChange={(e) => setHost(e.target.value)}
          required
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">SSH port</span>
        <input
          type="number"
          value={sshPort}
          onChange={(e) => setSshPort(Number(e.target.value))}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">WG port</span>
        <input
          type="number"
          value={wgPort}
          onChange={(e) => setWgPort(Number(e.target.value))}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col col-span-2">
        <span className="text-slate-400 mb-1">WG address (CIDR)</span>
        <input
          value={wgAddress}
          onChange={(e) => setWgAddress(e.target.value)}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
        />
      </label>
      <label className="flex flex-col col-span-2">
        <span className="text-slate-400 mb-1">WG public key (optional — use keygen кнопку)</span>
        <input
          value={wgPublic}
          onChange={(e) => setWgPublic(e.target.value)}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
        />
      </label>
      <label className="flex flex-col col-span-2">
        <span className="text-slate-400 mb-1">
          WG private key {isEdit && "(оставь пустым чтобы не менять)"}
        </span>
        <input
          type="password"
          value={wgPrivate}
          onChange={(e) => setWgPrivate(e.target.value)}
          placeholder={isEdit && editExit?.has_private_key ? "••••••••" : ""}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Cloud provider</span>
        <select
          value={providerId}
          onChange={(e) => setProviderId(e.target.value)}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        >
          <option value="">—</option>
          {providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name} ({p.kind})
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Provider region</span>
        <input
          value={providerRegion}
          onChange={(e) => setProviderRegion(e.target.value)}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col col-span-2">
        <span className="text-slate-400 mb-1">Provider external id</span>
        <input
          value={providerExternalId}
          onChange={(e) => setProviderExternalId(e.target.value)}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
        />
      </label>
      {isEdit && (
        <label className="flex flex-col">
          <span className="text-slate-400 mb-1">Status</span>
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
          >
            {STATUSES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>
      )}
      <label className="flex items-center gap-2 self-end">
        <input
          type="checkbox"
          checked={isActive}
          onChange={(e) => setIsActive(e.target.checked)}
        />
        <span className="text-slate-400">Active</span>
      </label>
      <label className="flex flex-col col-span-2">
        <span className="text-slate-400 mb-1">Notes</span>
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={2}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
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
