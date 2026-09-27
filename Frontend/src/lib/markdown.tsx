import { memo, useMemo, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Copy } from "lucide-react";

interface Props {
  text: string;
  className?: string;
  streaming?: boolean;
}

export const Markdown = memo(function Markdown({ text, className, streaming }: Props) {
  const components = useMemo(
    () => ({
      pre({ children }: { children?: ReactNode }) {
        const child = Array.isArray(children) ? children[0] : children;
        const el = child as React.ReactElement<{ className?: string; children?: ReactNode }> | undefined;
        const className = el?.props?.className || "";
        const lang = className.replace(/^language-/, "");
        const code = typeof el?.props?.children === "string" ? el.props.children : "";
        if (code) {
          return (
            <pre className="md-pre">
              <div className="md-pre__bar">
                <span className="md-pre__lang">{lang || "code"}</span>
                <button
                  className="md-pre__copy"
                  onClick={() => {
                    try {
                      navigator.clipboard.writeText(code);
                    } catch {
                      /* noop */
                    }
                  }}
                  aria-label="Copy code"
                >
                  <Copy size={13} />
                </button>
              </div>
              <code className={className}>{el?.props?.children ?? ""}</code>
            </pre>
          );
        }
        return <pre>{children}</pre>;
      },
      a({ href, children }: { href?: string; children?: ReactNode }) {
        return (
          <a href={href} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
            {children}
          </a>
        );
      },
    }),
    []
  );

  return (
    <div className={`md${className ? " " + className : ""}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {streaming ? `${text} ` : text}
      </ReactMarkdown>
    </div>
  );
});