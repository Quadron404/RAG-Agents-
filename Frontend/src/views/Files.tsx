import { useEffect, useState } from "react";
import {
  File,
  FileText,
  Folder,
  FolderOpen,
  HardDrive,
  Home,
  Image as ImageIcon,
  RefreshCw,
  Terminal,
} from "lucide-react";
import { useCore } from "../core";
import { fmtBytes } from "../lib/format";
import { haptic } from "../lib/haptics";
import { FilePreview, kindFor } from "../components/FilePreview";

interface Entry {
  name: string;
  isDir: boolean;
  size: number;
  mtime: string;
}

const FAVS: { label: string; path: string; icon: typeof Folder }[] = [
  { label: "Workspace", path: "/workspace", icon: FolderOpen },
  { label: "Backend root", path: ".", icon: Terminal },
  { label: "Home", path: "C:/Users", icon: Home },
  { label: "Drive C:", path: "C:/", icon: HardDrive },
];

function parseListing(output: string): Entry[] {
  const out: Entry[] = [];
  for (const line of output.split("\n")) {
    const cols = line.split("\t");
    if (!cols[0]) continue;
    if (cols[0] === "dir") {
      out.push({ name: cols[cols.length - 1], isDir: true, size: 0, mtime: cols[1] || "" });
    } else if (cols[0] === "file") {
      out.push({ name: cols[cols.length - 1], isDir: false, size: Number(cols[1]) || 0, mtime: cols[2] || "" });
    }
  }
  return out;
}

/** One predicate, shared by the grid's icon colours and the preview's renderer. */
function isImage(name: string) {
  return kindFor(name) === "image";
}

function isText(name: string) {
  return kindFor(name) === "text";
}

export function FilesPane({ embedded = false }: { embedded?: boolean }) {
  const callTool = useCore((s) => s.callTool);
  const setVmPath = useCore((s) => s.setVmPath);
  const vmPath = useCore((s) => s.vmPath);

  const [entries, setEntries] = useState<Entry[] | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [path, setPath] = useState(vmPath || "/workspace");
  const [preview, setPreview] = useState<{ path: string; name: string } | null>(null);
  const [sel, setSel] = useState<string | null>(null);

  const refresh = async (p: string) => {
    setLoading(true);
    setError("");
    setEntries(null);
    const r = await callTool("list_dir", { path: p }, 30);
    setLoading(false);
    if (r.error) {
      setError(r.error);
      return;
    }
    setEntries(parseListing(r.output));
  };

  useEffect(() => {
    refresh(path);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const open = (e: Entry) => {
    setSel(e.name);
    haptic("light");
    if (e.isDir) {
      const next = path.replace(/[\\/]+$/, "") + "/" + e.name;
      setPath(next);
      setVmPath(next);
      refresh(next);
      return;
    }
    // The preview fetches the bytes itself over the authenticated /file route,
    // so nothing is copied or re-encoded on the way in and the original stays
    // downloadable untouched.
    setPreview({ path: path + "/" + e.name, name: e.name });
  };

  const crumbs = path.split(/[\\/]+/).filter(Boolean);

  const openFav = (p: string) => {
    setPath(p);
    setVmPath(p);
    refresh(p);
  };

  return (
    <div className={`vmfiles${embedded ? " fs-embedded" : ""}`}>
      <div className="fs-fav">
        <div className="fs-fav__sec">Favorites</div>
        {FAVS.map((f) => {
          const Icon = f.icon;
          const on = path === f.path;
          return (
            <div key={f.path} className={`fs-fav__it${on ? " fs-fav__it--on" : ""}`} onClick={() => openFav(f.path)}>
              <Icon size={14} />
              {f.label}
            </div>
          );
        })}
        <div className="fs-fav__sec">Current</div>
        <div className="fs-fav__it">
          <Terminal size={14} />
          <span className="mono">{path}</span>
        </div>
      </div>

      <div className="fs-main">
        <div className="fs-crumbs">
          {crumbs.length ? (
            <>
              <span className="crumb" onClick={() => openFav("/")}>
                /
              </span>
              <span className="crumb-sep">›</span>
            </>
          ) : null}
          {crumbs.map((c, i) => {
            const upTo = "/" + crumbs.slice(0, i + 1).join("/");
            return (
              <span key={i}>
                <span className={`crumb${i === crumbs.length - 1 ? " crumb--cur" : ""}`} onClick={() => openFav(upTo)}>
                  {c}
                </span>
                {i < crumbs.length - 1 ? <span className="crumb-sep">›</span> : null}
              </span>
            );
          })}
          <span style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
            <button className="act-btn" onClick={() => refresh(path)} title="Refresh" aria-label="Refresh">
              <RefreshCw size={14} />
            </button>
          </span>
        </div>

        {loading ? (
          <div className="fs-load">
            <span className="spinner" />
            Reading {path}…
          </div>
        ) : error ? (
          <div className="fs-load" style={{ color: "var(--text-2)" }}>
            <Folder size={28} style={{ opacity: 0.4 }} />
            {error}
          </div>
        ) : !entries?.length ? (
          <div className="fs-grid">
            <div className="fs-empty">This folder is empty.</div>
          </div>
        ) : (
          <div className="fs-grid">
            {entries.map((e) => (
              <div
                key={e.name}
                className={`fs-cell${sel === e.name ? " fs-cell--sel" : ""}`}
                onClick={() => open(e)}
                onDoubleClick={() => e.isDir && open(e)}
                title={e.name}
              >
                <span
                  className="fs-cell__ic"
                  style={{
                    background: e.isDir
                      ? "var(--bot-navigator)"
                      : isImage(e.name)
                        ? "var(--bot-researcher)"
                        : isText(e.name)
                          ? "var(--bot-engineer)"
                          : "var(--bg4)",
                    color: e.isDir ? "#fff" : isText(e.name) ? "var(--text)" : "var(--text-2)",
                  }}
                >
                  {e.isDir ? <Folder size={22} /> : isImage(e.name) ? <ImageIcon size={22} /> : isText(e.name) ? <FileText size={22} /> : <File size={22} />}
                </span>
                <span className="fs-cell__nm">{e.name}</span>
                <span className="fs-cell__sz">{e.isDir ? "folder" : fmtBytes(e.size)}</span>
              </div>
            ))}
          </div>
        )}

        {preview ? (
          <FilePreview path={preview.path} name={preview.name} onClose={() => setPreview(null)} />
        ) : null}
      </div>
    </div>
  );
}

export function FilesView() {
  return (
    <div className="files-view">
      <FilesPane />
    </div>
  );
}