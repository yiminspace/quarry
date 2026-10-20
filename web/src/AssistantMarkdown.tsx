import Markdown, { type Components, type UrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import { createLowlight } from "lowlight";
import sql from "highlight.js/lib/languages/sql";
import { toJsxRuntime } from "hast-util-to-jsx-runtime";
import { Fragment, jsx, jsxs } from "react/jsx-runtime";

// Import only the SQL grammar. Other languages remain readable plain code.
const highlighter = createLowlight({ sql });
const sqlLanguages = new Set(["sql", "postgres", "postgresql", "pgsql", "mysql", "sqlite"]);

// Model output may contain links, but cannot navigate to local app routes or
// executable protocols. Images are rendered as text without requesting a URL.
const safeUrl: UrlTransform = (url, key) => {
  if (key !== "href" || /\p{Cc}/u.test(url)) return undefined;
  try {
    return ["http:", "https:", "mailto:"].includes(new URL(url).protocol) ? url : undefined;
  } catch {
    return undefined;
  }
};

const components: Components = {
  a: ({ href, title, children }) => href
    ? <a href={href} title={title} target="_blank" rel="noopener noreferrer">{children}</a>
    : <span>{children}</span>,
  img: ({ alt }) => <span className="assistant-image-alt">{alt}</span>,
  table: ({ children }) => <div className="assistant-table-scroll"><table>{children}</table></div>,
  code: ({ className, children }) => {
    const language = className?.split(/\s+/).find((name) => name.startsWith("language-"))?.slice(9).toLowerCase();
    const highlighted = language && sqlLanguages.has(language) && typeof children === "string"
      ? toJsxRuntime(highlighter.highlight("sql", children), { Fragment, jsx, jsxs })
      : children;
    return <code className={className}>{highlighted}</code>;
  },
};

export default function AssistantMarkdown({ children }: { children: string }) {
  return <div className="assistant-markdown">
    <Markdown remarkPlugins={[remarkGfm]} components={components} urlTransform={safeUrl} skipHtml>{children}</Markdown>
  </div>;
}
