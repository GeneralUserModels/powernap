/**
 * useMomentEditor — direct Codex/Claude editor chat for generated Tada apps.
 */

import { useCallback, useEffect, useRef } from "react";
import { getServerUrl } from "../../shared/api-core";
import { useChat } from "./useChat";

export type MomentDraftPatch = Record<string, unknown>;
export type MomentDraftSnapshot = Record<string, unknown>;

export type MomentEditorHandlers = {
  onDraftPatch?: (patch: MomentDraftPatch) => void | Promise<void>;
  onRevision?: (revision: string) => void;
  onChangedFiles?: (files: string[]) => void;
};

type MomentEditorConversation = {
  active?: boolean;
  messages?: ChatMessage[];
};

export function useMomentEditor(slug: string, handlers: MomentEditorHandlers = {}) {
  const handlersRef = useRef(handlers);

  useEffect(() => {
    handlersRef.current = handlers;
  }, [handlers]);

  const chat = useChat({
    apiPrefix: `/api/moments/${slug}/editor`,
    onEvent: (data) => {
      const draftPatch = data.draft_patch;
      if (draftPatch && typeof draftPatch === "object" && !Array.isArray(draftPatch)) {
        void handlersRef.current.onDraftPatch?.(draftPatch as MomentDraftPatch);
      }
      if (typeof data.revision === "string") {
        handlersRef.current.onRevision?.(data.revision);
      }
      if (Array.isArray(data.changed_files)) {
        handlersRef.current.onChangedFiles?.(data.changed_files.map(String));
      }
    },
  });

  useEffect(() => {
    if (!slug) return;
    let cancelled = false;
    const controller = new AbortController();

    fetch(`${getServerUrl()}/api/moments/${encodeURIComponent(slug)}/editor/conversation`, {
      signal: controller.signal,
    })
      .then(async (res) => {
        if (!res.ok) return null;
        return await res.json() as MomentEditorConversation;
      })
      .then((data) => {
        if (cancelled || !data) return;
        chat.setMessages(Array.isArray(data.messages) ? data.messages : []);
        chat.setActive(Boolean(data.active));
      })
      .catch((err) => {
        if (err instanceof Error && err.name === "AbortError") return;
        console.error("[moment-editor] conversation load failed:", err);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [slug, chat.setMessages, chat.setActive]);

  const prepare = useCallback(async () => {
    if (!slug) return null;
    const res = await fetch(`${getServerUrl()}/api/moments/${encodeURIComponent(slug)}/editor/prepare`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || `Prepare failed: ${res.status}`);
    }
    const data = await res.json() as { prepared: boolean; revision: string };
    handlersRef.current.onRevision?.(data.revision);
    return data;
  }, [slug]);

  return {
    ...chat,
    prepare,
  };
}
