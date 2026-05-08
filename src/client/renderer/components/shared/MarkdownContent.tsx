import React from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";

/** Strip YAML frontmatter from markdown text before rendering. */
export function stripFrontmatter(md: string): string {
  if (!md.startsWith("---")) return md;
  const end = md.indexOf("---", 3);
  if (end === -1) return md;
  return md.slice(end + 3).trimStart();
}

interface MarkdownContentProps {
  markdown: string;
  className?: string;
  components?: Components;
  processMarkdown?: (markdown: string) => string;
}

export function MarkdownContent({
  markdown,
  className,
  components,
  processMarkdown = (value) => value,
}: MarkdownContentProps) {
  const mergedComponents: Components = {
    a: ({ href, children }) => (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    ),
    ...components,
  };
  const rendered = processMarkdown(stripFrontmatter(markdown));

  return (
    <div className={className}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw]}
        urlTransform={(url) => url}
        components={mergedComponents}
      >
        {rendered}
      </ReactMarkdown>
    </div>
  );
}
