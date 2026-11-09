import { createMarkdownRenderer, type MarkdownRenderer } from "./render";

export interface SessionCodeHighlighter {
  codeToHtml(
    source: string,
    options: { lang: string; theme: "github-dark" }
  ): string;
}

export type SessionHighlighterLoader = () => Promise<SessionCodeHighlighter | null>;

export interface SessionMarkdownRenderer {
  render: MarkdownRenderer;
  clearCache: () => void;
}

const THEME = "github-dark";
const MAX_HIGHLIGHT_BYTES = 64 * 1024;
const MAX_HIGHLIGHT_LINES = 1000;
const MAX_CACHE_ENTRIES = 128;
const MAX_CACHE_BYTES = 4 * 1024 * 1024;

const LANGUAGE_ALIASES: Record<string, string> = {
  py: "python",
  python3: "python",
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  node: "javascript",
  ts: "typescript",
  mts: "typescript",
  cts: "typescript",
  vue: "vue",
  html: "html",
  htm: "html",
  css: "css",
  sh: "bash",
  shell: "bash",
  zsh: "bash",
  json: "json",
  jsonc: "jsonc",
  yml: "yaml",
  yaml: "yaml",
  toml: "toml",
  md: "markdown",
  markdown: "markdown",
  sql: "sql",
  diff: "diff",
  patch: "diff",
  go: "go",
  golang: "go",
  rs: "rust",
  rust: "rust",
  docker: "dockerfile",
  dockerfile: "dockerfile",
};

const SUPPORTED_LANGUAGES = new Set([
  "python",
  "javascript",
  "typescript",
  "jsx",
  "tsx",
  "vue",
  "html",
  "css",
  "bash",
  "json",
  "jsonc",
  "yaml",
  "toml",
  "markdown",
  "sql",
  "diff",
  "go",
  "rust",
  "dockerfile",
]);

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function byteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function exceedsHighlightLimit(source: string): boolean {
  if (source.length > MAX_HIGHLIGHT_BYTES) return true;

  let lines = 1;
  for (let index = 0; index < source.length; index += 1) {
    if (source.charCodeAt(index) === 10) lines += 1;
  }
  if (source.endsWith("\n")) lines -= 1;
  if (lines > MAX_HIGHLIGHT_LINES) return true;

  return byteLength(source) > MAX_HIGHLIGHT_BYTES;
}

function normalizeLanguage(language: string): string | null {
  const name = language.trim().toLowerCase().split(/\s+/, 1)[0] ?? "";
  const normalized = LANGUAGE_ALIASES[name] ?? name;
  return SUPPORTED_LANGUAGES.has(normalized) ? normalized : null;
}

async function loadShikiHighlighter(): Promise<SessionCodeHighlighter> {
  const [
    { createHighlighterCore },
    { createJavaScriptRegexEngine },
    { default: githubDark },
    { default: python },
    { default: javascript },
    { default: typescript },
    { default: jsx },
    { default: tsx },
    { default: vue },
    { default: html },
    { default: css },
    { default: bash },
    { default: json },
    { default: jsonc },
    { default: yaml },
    { default: toml },
    { default: markdown },
    { default: sql },
    { default: diff },
    { default: go },
    { default: rust },
    { default: dockerfile },
  ] = await Promise.all([
    import("shiki/core"),
    import("shiki/engine/javascript"),
    import("@shikijs/themes/github-dark"),
    import("@shikijs/langs/python"),
    import("@shikijs/langs/javascript"),
    import("@shikijs/langs/typescript"),
    import("@shikijs/langs/jsx"),
    import("@shikijs/langs/tsx"),
    import("@shikijs/langs/vue"),
    import("@shikijs/langs/html"),
    import("@shikijs/langs/css"),
    import("@shikijs/langs/bash"),
    import("@shikijs/langs/json"),
    import("@shikijs/langs/jsonc"),
    import("@shikijs/langs/yaml"),
    import("@shikijs/langs/toml"),
    import("@shikijs/langs/markdown"),
    import("@shikijs/langs/sql"),
    import("@shikijs/langs/diff"),
    import("@shikijs/langs/go"),
    import("@shikijs/langs/rust"),
    import("@shikijs/langs/dockerfile"),
  ]);

  return createHighlighterCore({
    engine: createJavaScriptRegexEngine(),
    themes: [githubDark],
    langs: [
      python,
      javascript,
      typescript,
      jsx,
      tsx,
      vue,
      html,
      css,
      bash,
      json,
      jsonc,
      yaml,
      toml,
      markdown,
      sql,
      diff,
      go,
      rust,
      dockerfile,
    ],
  });
}

export async function createSessionMarkdownRenderer(
  loadHighlighter: SessionHighlighterLoader = loadShikiHighlighter
): Promise<SessionMarkdownRenderer> {
  let highlighter: SessionCodeHighlighter | null = null;
  try {
    highlighter = await loadHighlighter();
  } catch {
    highlighter = null;
  }

  const cache = new Map<string, { html: string; bytes: number }>();
  let cacheBytes = 0;

  function clearCache(): void {
    cache.clear();
    cacheBytes = 0;
  }

  function readCache(key: string): string | null {
    const entry = cache.get(key);
    if (!entry) return null;
    cache.delete(key);
    cache.set(key, entry);
    return entry.html;
  }

  function writeCache(key: string, html: string): void {
    const bytes = byteLength(key) + byteLength(html);
    if (bytes > MAX_CACHE_BYTES) return;

    const existing = cache.get(key);
    if (existing) {
      cacheBytes -= existing.bytes;
      cache.delete(key);
    }

    while (cache.size >= MAX_CACHE_ENTRIES) {
      const oldest = cache.keys().next().value;
      if (oldest === undefined) break;
      const removed = cache.get(oldest);
      cache.delete(oldest);
      if (removed) cacheBytes -= removed.bytes;
    }
    while (cacheBytes + bytes > MAX_CACHE_BYTES) {
      const oldest = cache.keys().next().value;
      if (oldest === undefined) break;
      const removed = cache.get(oldest);
      cache.delete(oldest);
      if (removed) cacheBytes -= removed.bytes;
    }

    cache.set(key, { html, bytes });
    cacheBytes += bytes;
  }

  const render = createMarkdownRenderer((source, language) => {
    const normalized = normalizeLanguage(language);
    if (!highlighter || !normalized || exceedsHighlightLimit(source)) {
      return escapeHtml(source);
    }

    const key = `${normalized}\u0000${source}`;
    const cached = readCache(key);
    if (cached !== null) return cached;

    try {
      const html = highlighter.codeToHtml(source, { lang: normalized, theme: THEME });
      writeCache(key, html);
      return html;
    } catch {
      return escapeHtml(source);
    }
  });

  return { render, clearCache };
}
