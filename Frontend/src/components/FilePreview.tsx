import { useCallback, useEffect, useRef, useState } from "react";
import {
  Download,
  FileQuestion,
  FileText,
  Image as ImageIcon,
  Loader2,
  Minus,
  Plus,
  RotateCcw,
  X,
} from "lucide-react";

import { api, apiFetch } from "../core";

/**
 * In-app file preview.
 *
 * The rules this follows: the bytes are never re-encoded, the user never leaves
 * the desktop, and anything that cannot be shown honestly is labelled as such.
 * Download is always present, including on the states that cannot render -- if
 * the app cannot show a file, the escape hatch is a download, not a dead end.
 *
 * Rendering is chosen per type rather than uniformly:
 *
 *   PDF   the browser's own viewer, in an <iframe>.  It already does page
 *         rendering, zoom and page navigation, and it is already on the machine
 *         as a plugin -- shipping a second PDF engine would mean a large
 *         dependency, a worker file and a decode cost on a machine whose CPU is
 *         busy running a browser, for a worse result.
 *   DOCX  mammoth, converted to HTML on demand.  There is no native renderer, so
 *         this is the one format that needs a library, and it is loaded the first
 *         time somebody actually opens a .docx.
 *   image a plain <img> with transform-based zoom and pan -- no relayout, so
 *         dragging a large photo stays on the compositor.
 *   text  <pre>, sized to the content and capped so a stray 40MB log cannot
 *         wedge the window.
 */

export type PreviewKind = "image" | "pdf" | "docx" | "text" | "unsupported";

const MAX_TEXT = 2 * 1024 * 1024;

const IMAGE_EXT = /\.(png|jpe?g|gif|webp|bmp|svg|avif)$/i;
const TEXT_EXT =
  /\.(txt|md|mdx|json|ya?ml|toml|ini|cfg|conf|env|log|csv|tsv|xml|html?|css|scss|less|py|js|mjs|cjs|ts|tsx|jsx|go|rs|rb|java|kt|kts|swift|c|h|cc|cpp|hpp|cs|php|sh|bash|zsh|ps1|bat|cmd|sql|graphql|vue|svelte|lua|vim|dockerfile|makefile)$/i;
const DOCX_EXT = /\.docx$/i;
const PDF_EXT = /\.pdf$/i;

/** The human-readable type, shown when we cannot render the file. */
export function describeType(name: string): string {
  if (PDF_EXT.test(name)) return "PDF document";
  if (DOCX_EXT.test(name)) return "Word document";
  if (IMAGE_EXT.test(name)) return "Image";
  if (TEXT_EXT.test(name)) return "Text file";
  const m = /\.([a-z0-9]{1,8})$/i.exec(name);
  return m ? `${m[1].toUpperCase()} file` : "File";
}

export function kindFor(name: string): PreviewKind {
  if (IMAGE_EXT.test(name)) return "image";
  if (PDF_EXT.test(name)) return "pdf";
  if (DOCX_EXT.test(name)) return "docx";
  if (TEXT_EXT.test(name)) return "text";
  return "unsupported";
}

const fileUrl = (path: string) => api(`/file?path=${encodeURIComponent(path)}`);
const downloadUrl = (path: string) => api(`/file?path=${encodeURIComponent(path)}&download=1`);

/* ---- image: zoom + pan, entirely on the compositor ------------------------ */

function ImagePreview({ path, name }: { path: string; name: string }) {
  const [zoom, setZoom] = useState(1);
  const [fit, setFit] = useState(true);
  const [broken, setBroken] = useState(false);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null);

  const onWheel = useCallback((e: React.WheelEvent) => {
    // Zoom about the pointer, which is what makes this feel like a real viewer
    // rather than a slider that is hard to aim.
    e.preventDefault();
    const k = Math.exp(-e.deltaY * 0.0015);
    setFit(false);
    setZoom((z) => Math.min(8, Math.max(0.1, z * k)));
  }, []);

  const reset = () => {
    setFit(true);
    setZoom(1);
    setOffset({ x: 0, y: 0 });
  };

  if (broken) {
    return (
      <div className="fp-blank">
        <FileQuestion size={30} />
        <p>The image could not be decoded.</p>
        <span className="set-dim">{name}</span>
      </div>
    );
  }

  return (
    <div
      className={`fp-canvas${drag.current ? " fp-canvas--drag" : ""}`}
      onWheel={onWheel}
      onPointerDown={(e) => {
        if (fit) return;
        drag.current = { x: e.clientX, y: e.clientY, ox: offset.x, oy: offset.y };
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        if (!drag.current) return;
        setOffset({
          x: drag.current.ox + (e.clientX - drag.current.x),
          y: drag.current.oy + (e.clientY - drag.current.y),
        });
      }}
      onPointerUp={(e) => {
        drag.current = null;
        e.currentTarget.releasePointerCapture(e.pointerId);
      }}
      onDoubleClick={reset}
    >
      <img
        src={fileUrl(path)}
        alt={name}
        onError={() => setBroken(true)}
        style={fit ? undefined : { transform: `translate3d(${offset.x}px, ${offset.y}px, 0) scale(${zoom})` }}
        draggable={false}
      />
      <div className="fp-zoom">
        <button onClick={() => setZoom((z) => Math.max(0.1, z / 1.25))} title="Zoom out" aria-label="Zoom out">
          <Minus size={14} />
        </button>
        <span className="mono">{Math.round(zoom * 100)}%</span>
        <button onClick={() => setZoom((z) => Math.min(8, z * 1.25))} title="Zoom in" aria-label="Zoom in">
          <Plus size={14} />
        </button>
        <button onClick={reset} title="Fit to window" aria-label="Fit to window">
          <RotateCcw size={13} />
        </button>
      </div>
    </div>
  );
}

/* ---- docx: converted on demand -------------------------------------------- */

function DocxPreview({ path, name }: { path: string; name: string }) {
  const [html, setHtml] = useState<string | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const res = await apiFetch(`/file?path=${encodeURIComponent(path)}`);
        if (!res.ok) throw new Error(`the server returned ${res.status}`);
        const buffer = await res.arrayBuffer();
        // The import is the expensive part, so it happens here, on first use,
        // rather than in the bundle every page load pays for.
        const mod: any = await import("mammoth");
        const mammoth = mod.default ?? mod;
        const result = await mammoth.convertToHtml({ arrayBuffer: buffer });
        if (live) setHtml(String(result.value));
      } catch (e) {
        if (live) setErr(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      live = false;
    };
  }, [path]);

  if (err) {
    return (
      <div className="fp-blank">
        <FileQuestion size={30} />
        <p>This document could not be rendered.</p>
        <span className="set-dim">{err}</span>
      </div>
    );
  }
  if (html === null) {
    return (
      <div className="fp-blank">
        <Loader2 size={26} className="fp-spin" />
        <p>Converting {name}…</p>
      </div>
    );
  }
  // mammoth emits a document fragment; the wrapper class scopes its own styles
  // so a document's headings cannot restyle the desktop behind it.
  return <div className="fp-docx" dangerouslySetInnerHTML={{ __html: html }} />;
}

/* ---- pdf: the browser's own renderer ------------------------------------- */

function PdfPreview({ path, name }: { path: string; name: string }) {
  return (
    <iframe
      className="fp-pdf"
      src={fileUrl(path)}
      title={`Preview of ${name}`}
    />
  );
}

/* ---- the overlay ---------------------------------------------------------- */

export function FilePreview({
  path,
  name,
  onClose,
}: {
  path: string;
  name: string;
  onClose: () => void;
}) {
  const kind = kindFor(name);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fp" role="dialog" aria-label={`Preview of ${name}`}>
      <div className="fp__head">
        <span className="fp__icon">
          {kind === "image" ? <ImageIcon size={15} /> : <FileText size={15} />}
        </span>
        <span className="fp__name" title={name}>
          {name}
        </span>
        <span className="fp__tag">{describeType(name)}</span>
        <div className="fp__acts">
          <a className="btn btn--ghost btn--sm" href={downloadUrl(path)} download={name} title="Download the original file">
            <Download size={14} /> Download
          </a>
          <button className="icon-btn" onClick={onClose} title="Close preview" aria-label="Close preview">
            <X size={15} />
          </button>
        </div>
      </div>

      <div className="fp__body">
        {kind === "image" ? <ImagePreview path={path} name={name} /> : null}
        {kind === "pdf" ? <PdfPreview path={path} name={name} /> : null}
        {kind === "docx" ? <DocxPreview path={path} name={name} /> : null}
        {kind === "text" ? <TextPreview path={path} /> : null}
        {kind === "unsupported" ? (
          <div className="fp-blank">
            <FileQuestion size={30} />
            <p>Preview unavailable</p>
            <span className="set-dim">
              {describeType(name)}s cannot be shown here. Download it to open it in another app.
            </span>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function TextPreview({ path }: { path: string }) {
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const res = await apiFetch(`/file?path=${encodeURIComponent(path)}`);
        if (!res.ok) throw new Error(`the server returned ${res.status}`);
        const raw = await res.text();
        if (!live) return;
        // Cap the render, not the download: a window that tries to lay out a
        // 40MB log stops being interactive for everybody, including the live
        // screen sharing the same process.
        setText(raw.length > MAX_TEXT ? `${raw.slice(0, MAX_TEXT)}\n\n… truncated at ${MAX_TEXT} characters …` : raw);
      } catch (e) {
        if (live) setErr(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      live = false;
    };
  }, [path]);

  if (err) {
    return (
      <div className="fp-blank">
        <FileQuestion size={30} />
        <p>This file could not be read.</p>
        <span className="set-dim">{err}</span>
      </div>
    );
  }
  if (text === null) {
    return (
      <div className="fp-blank">
        <Loader2 size={26} className="fp-spin" />
        <p>Reading…</p>
      </div>
    );
  }
  return <pre className="fp-text">{text}</pre>;
}
